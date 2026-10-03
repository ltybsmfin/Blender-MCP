"""Blender MCP bridge add-on / server side.

Self-contained: this module imports **only** the Python standard library that
ships inside Blender plus ``bpy`` (and ``mathutils``, also bundled).  It never
imports the ``mcp`` package, so it works in a stock Blender installation.

Design
------
* A tiny line-delimited JSON protocol over TCP on ``127.0.0.1`` (default port
  ``9876``), authenticated with a shared token file (default
  ``<workspace>/MCP/.runtime/token``).  The token is never logged or echoed.
* The listening socket is **non-blocking**.  In the GUI a ``bpy.app.timers``
  callback polls it; every request is executed on Blender's *main* thread.
  There are no worker threads and nothing but the main thread touches ``bpy``.
* Headless (``blender --background``) has no event loop, so ``bootstrap.py``
  pumps the same ``poll()`` entry point from a plain loop instead.  This is the
  documented headless harness path.
* Mutations are never replayed.  A malformed/oversized frame, a bad token or a
  failing command produces an explicit error response, never a silent success.

Commands are grouped by the feature they expose and are documented in the
project ``README.md``.  See ``COMMANDS`` at the bottom of this module.
"""

from __future__ import annotations

import inspect
import json
import ast
import ipaddress
import keyword
import math
import os
import secrets
import socket
import stat
import sys
import time
import traceback
from pathlib import Path

import bpy
bl_info = {
    "name": "Blender MCP Bridge",
    "author": "Blender MCP workstream",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "Properties > Scene > Blender MCP  |  bpy.ops.blendermcp.start_bridge()",
    "description": "Local JSON bridge (127.0.0.1, token authenticated) for an MCP stdio server",
    "category": "Development",
}

PROTOCOL_VERSION = 1
ADDON_VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_OUT_BUFFER_BYTES = 8 * 1024 * 1024
DEFAULT_TIMER_INTERVAL = 0.05
TOKEN_BYTES = 32

# Per-poll budgets: the socket loop runs on Blender's main thread, so a single
# client (or a burst of pipelined frames) must never be able to hold the UI for
# long.  Work left over stays queued and is picked up by the next poll.
MAX_CLIENTS = 8
MAX_ACCEPTS_PER_POLL = 4
MAX_BYTES_PER_POLL_PER_CLIENT = 256 * 1024
MAX_MESSAGES_PER_POLL = 32
MAX_MESSAGES_PER_POLL_PER_CLIENT = 8
# After an error response the peer may still be pushing the rest of a bad frame.
# Closing with unread inbound data makes the OS send RST and DISCARD our queued
# answer, so the socket is half-closed (SHUT_WR) and drained briefly instead.
LINGER_MAX_BYTES = 64 * 1024
LINGER_MAX_POLLS = 5

# Numeric socket types that Blender converts implicitly when linking.
_NUMERIC_SOCKET_TYPES = {"FLOAT", "INT", "VECTOR", "COLOR", "ROTATION", "BOOLEAN"}
_SOCKET_COMPATIBLE = {
    ("GEOMETRY", "GEOMETRY"),
    ("STRING", "STRING"),
    ("MENU", "MENU"),
}

_DRIVER_EXPRESSION_BUILTINS = {
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh",
    "sqrt", "pow", "log", "log10", "exp", "abs", "floor", "ceil", "round", "trunc",
    "min", "max", "fmod", "hypot", "radians", "degrees", "pi", "e",
    "True", "False", "None", "int", "float", "len",
}
# Character-level rejections (statements / escapes are never valid in a driver expression).
_DRIVER_EXPRESSION_FORBIDDEN_CHARS = (";", "\n", "\r", "\\", "`", "#", "@", "!", "$")
# Identifier-level rejections.  Driver expressions are evaluated by Blender's own
# restricted expression evaluator; this list keeps obviously-executable Python out
# of the graph while still allowing comparisons and arithmetic.
_DRIVER_EXPRESSION_FORBIDDEN_NAMES = {
    "import", "eval", "exec", "open", "compile", "lambda", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir", "input", "breakpoint", "memoryview",
    "class", "def", "return", "while", "for", "if", "elif", "else", "with", "as",
    "try", "except", "finally", "raise", "yield", "global", "nonlocal", "from",
    "pass", "del", "assert", "async", "await", "print", "help", "exit", "quit",
}
_DRIVER_ID_COLLECTIONS = {
    "OBJECT": "objects",
    "SCENE": "scenes",
    "KEY": "shape_keys",
    "MESH": "meshes",
    "MATERIAL": "materials",
    "NODETREE": "node_groups",
    "COLLECTION": "collections",
    "ARMATURE": "armatures",
    "CAMERA": "cameras",
    "LIGHT": "lights",
    "WORLD": "worlds",
    "IMAGE": "images",
    "TEXT": "texts",
    "ACTION": "actions",
    "META": "metaballs",
    "LATTICE": "lattices",
    "CURVE": "curves",
    "VOLUME": "volumes",
    "POINTCLOUD": "pointclouds",
    "GREASEPENCIL": "grease_pencils",
    "PALETTE": "palettes",
    "PAINT_CURVE": "paint_curves",
    "MOVIECLIP": "movieclips",
    "MASK": "masks",
    "SOUND": "sounds",
    "FONT": "fonts",
    "CACHEFILE": "cache_files",
    "LIBRARY": "libraries",
    "VERSION": "version",
    "WINDOWMANAGER": "window_managers",
}


# --------------------------------------------------------------------------- #
# errors, logging, json helpers
# --------------------------------------------------------------------------- #
class CommandError(Exception):
    """An explicit, user-facing failure of one bridge command."""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_payload(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = _jsonable(self.details)
        return payload


def _jsonable(value):
    """Best-effort conversion of bpy/mathutils values into JSON-friendly types."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "to_dict"):  # IDPropertyGroup
        try:
            return {str(k): _jsonable(v) for k, v in value.to_dict().items()}
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "to_tuple"):  # mathutils Vector/Matrix/Euler/Quaternion
        try:
            return [float(x) for x in value.to_tuple()]
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list,)):  # pragma: no cover - defensive
        return [_jsonable(v) for v in value]
    try:
        return [float(x) for x in value]
    except Exception:  # noqa: BLE001
        return str(value)


class _Logger:
    """Append-only, size-capped log file plus stderr echo. Never logs secrets."""

    def __init__(self, path: Path | None = None, cap: int = 1 * 1024 * 1024):
        self.path = path
        self.cap = cap

    def __call__(self, message: str) -> None:
        line = "[blender-mcp %s] %s" % (time.strftime("%H:%M:%S"), message)
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        if not self.path:
            return
        try:
            if self.path.exists() and self.path.stat().st_size > self.cap:
                self.path.write_text("", encoding="utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# paths and token handling
# --------------------------------------------------------------------------- #
def addon_dir() -> Path:
    return Path(__file__).resolve().parent


def workspace_root() -> Path:
    override = os.environ.get("BLENDER_MCP_WORKSPACE")
    if override:
        return Path(override).resolve()
    # <workspace>/MCP/blender/blender_mcp_addon.py -> addon_dir() is already .../MCP/blender
    return addon_dir().parents[1]


def mcp_dir() -> Path:
    return Path(os.environ.get("BLENDER_MCP_HOME") or (workspace_root() / "MCP")).resolve()


def runtime_dir() -> Path:
    return mcp_dir() / ".runtime"


def canonical_project_dir() -> Path:
    """The canonical sibling ``<workspace>/Project`` directory."""
    return workspace_root() / "Project"


def project_dir() -> Path:
    """Project root for save_blend, honouring an in-tree override only.

    ``BLENDER_MCP_PROJECT_DIR`` may point at a subdirectory of the canonical
    sibling ``<workspace>/Project`` (tests do this), but an override that escapes
    it - or that reaches it through a symlink/junction - is rejected outright.
    """
    override = os.environ.get("BLENDER_MCP_PROJECT_DIR")
    canonical = canonical_project_dir()
    if canonical.exists() and _is_reparse_point(canonical):
        raise CommandError(
            "invalid_project_dir",
            "the canonical project directory is a symlink/junction, which is not accepted",
            details={"canonical": str(canonical)},
        )
    if not override:
        return canonical
    canonical_real = Path(os.path.realpath(str(canonical)))
    candidate = Path(override)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Check the path the caller supplied before realpath removes junctions.
    if not _within(str(candidate), str(canonical)):
        raise CommandError("invalid_project_dir", "project override must stay inside Project")
    walk = canonical
    for part in candidate.relative_to(canonical).parts:
        walk = walk / part
        if _is_reparse_point(walk):
            raise CommandError("invalid_project_dir", "project override contains a symlink or junction")
    candidate_real = Path(os.path.realpath(str(candidate)))
    if not _within(str(candidate_real), str(canonical_real)):
        raise CommandError(
            "invalid_project_dir",
            "BLENDER_MCP_PROJECT_DIR must stay inside the canonical Project directory",
            details={"canonical": str(canonical_real), "requested": str(candidate_real)},
        )
    # Reject any reparse point between the canonical root and the override.
    try:
        relative = candidate_real.relative_to(canonical_real)
    except ValueError as exc:  # pragma: no cover - covered by the check above
        raise CommandError("invalid_project_dir", str(exc)) from exc
    walk = canonical_real
    for part in relative.parts:
        walk = walk / part
        if walk.exists() and _is_reparse_point(walk):
            raise CommandError(
                "invalid_project_dir",
                "BLENDER_MCP_PROJECT_DIR must not traverse a symlink/junction",
                details={"component": str(walk)},
            )
    return candidate_real


def default_token_path() -> Path:
    return runtime_dir() / "token"


def default_log_path() -> Path:
    return runtime_dir() / "logs" / "blender_bridge.log"


def read_or_create_token(path: Path | None = None) -> str:
    """Read the shared token, creating it (0600, best effort) when absent."""
    token_path = Path(path) if path else default_token_path()
    try:
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(TOKEN_BYTES)
        token_path.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        return token
    except OSError as exc:
        raise CommandError(
            "token_unavailable",
            "cannot read or create the bridge token file: %s" % exc,
            details={"token_path": str(token_path)},
        ) from exc


def read_token(path: Path | None = None) -> str:
    token_path = Path(path) if path else default_token_path()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CommandError(
            "token_unavailable",
            "bridge token file is unreadable: %s" % exc,
            details={"token_path": str(token_path)},
        ) from exc
    if not token:
        raise CommandError("token_unavailable", "bridge token file is empty")
    return token


def _is_reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and other reparse points."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _within(child: str, parent: str) -> bool:
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def resolve_project_path(relative_path: str) -> Path:
    """Resolve ``relative_path`` strictly inside ``<workspace>/Project``.

    Rejects absolute paths, ``..`` escapes, symlink/junction components and any
    path that resolves outside the project root.
    """
    if not relative_path or not str(relative_path).strip():
        raise CommandError("invalid_path", "path must not be empty")
    raw = str(relative_path).strip().replace("/", os.sep)
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise CommandError(
            "invalid_path",
            "path must be relative to <workspace>/Project, got an absolute path",
            details={"path": relative_path},
        )
    parts = [p for p in raw.split(os.sep) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise CommandError(
            "invalid_path",
            "path traversal ('..') is rejected",
            details={"path": relative_path},
        )
    if not parts:
        raise CommandError("invalid_path", "path does not name a file", details={"path": relative_path})

    root = project_dir().resolve()
    candidate = root.joinpath(*parts)

    walk = root
    for part in parts[:-1]:
        walk = walk / part
        if walk.exists() and _is_reparse_point(walk):
            raise CommandError(
                "invalid_path",
                "symlinked/junction directories are rejected",
                details={"path": relative_path, "component": str(walk)},
            )
    if candidate.exists() and _is_reparse_point(candidate):
        raise CommandError(
            "invalid_path",
            "symlinked target files are rejected",
            details={"path": relative_path},
        )
    real = Path(os.path.realpath(str(candidate)))
    if not _within(str(real), str(root)):
        raise CommandError(
            "path_outside_project",
            "resolved path escapes <workspace>/Project",
            details={"path": relative_path, "resolved": str(real), "project_root": str(root)},
        )
    return real


# --------------------------------------------------------------------------- #
# generic bpy helpers
# --------------------------------------------------------------------------- #
def get_object(name: str):
    obj = bpy.data.objects.get(str(name))
    if obj is None:
        raise CommandError(
            "object_not_found",
            "no object named %r" % name,
            details={"available": [o.name for o in bpy.data.objects][:50]},
        )
    return obj


def get_node_group(name: str):
    group = bpy.data.node_groups.get(str(name))
    if group is None:
        raise CommandError(
            "node_group_not_found",
            "no node group named %r" % name,
            details={"available": [g.name for g in bpy.data.node_groups][:50]},
        )
    return group


def get_mesh_object(name: str):
    obj = get_object(name)
    if obj.type != "MESH":
        raise CommandError(
            "wrong_object_type",
            "object %r is %s, expected MESH" % (obj.name, obj.type),
            details={"object": obj.name, "type": obj.type},
        )
    if obj.data is None:
        raise CommandError("no_mesh_data", "object %r has no mesh data" % obj.name)
    return obj


def get_node(group, ref):
    """Resolve a node by exact name, or by integer index."""
    if isinstance(ref, bool):
        raise CommandError("invalid_node_ref", "node reference must be a name or index")
    if isinstance(ref, int):
        nodes = list(group.nodes)
        if ref < 0 or ref >= len(nodes):
            raise CommandError(
                "node_not_found",
                "node index %d out of range (group has %d nodes)" % (ref, len(nodes)),
            )
        return nodes[ref]
    if isinstance(ref, str):
        node = group.nodes.get(ref)
        if node is not None:
            return node
    raise CommandError(
        "node_not_found",
        "no node %r in node group %r" % (ref, group.name),
        details={"available": [n.name for n in group.nodes]},
    )


def _socket_collection(node, in_out: str):
    in_out = (in_out or "INPUT").upper()
    if in_out in ("INPUT", "IN", "INPLACE"):
        return node.inputs
    if in_out in ("OUTPUT", "OUT"):
        return node.outputs
    raise CommandError("invalid_argument", "in_out must be 'INPUT' or 'OUTPUT', got %r" % in_out)


def _is_same_node_group(candidate, group) -> bool:
    """Compare two node-tree references by name (bpy wrappers are not identity-safe)."""
    if candidate is None or group is None:
        return False
    try:
        return candidate.name == group.name
    except AttributeError:
        return False


def _link_touches_node(link, node) -> bool:
    """Whether a link starts or ends on ``node``.

    Compares by name on purpose: ``bpy`` hands out fresh Python wrappers for the
    same RNA struct, so ``link.from_node is node`` can be False for a node that is
    in fact the same one (this silently broke link counting and unlinking).
    """
    try:
        return link.from_node.name == node.name or link.to_node.name == node.name
    except AttributeError:
        return False


def _link_touches_socket(link, node, socket, in_out: str) -> bool:
    try:
        if in_out.upper().startswith("IN"):
            return link.to_node.name == node.name and link.to_socket.identifier == socket.identifier
        return link.from_node.name == node.name and link.from_socket.identifier == socket.identifier
    except AttributeError:
        return False


def get_socket(node, ref, in_out: str = "INPUT", require_linked: bool | None = None):
    """Resolve a socket by identifier first, then by display name, then index."""
    sockets = _socket_collection(node, in_out)
    socket_obj = None
    if isinstance(ref, bool):
        raise CommandError("invalid_socket_ref", "socket reference must be a name, identifier or index")
    if isinstance(ref, int):
        if ref < 0 or ref >= len(sockets):
            raise CommandError(
                "socket_not_found",
                "%s index %d out of range on node %r (%d sockets)"
                % (in_out.lower(), ref, node.name, len(sockets)),
                details={"sockets": _socket_summary(sockets)},
            )
        socket_obj = sockets[ref]
    elif isinstance(ref, str):
        for candidate in sockets:
            if candidate.identifier == ref:
                socket_obj = candidate
                break
        if socket_obj is None:
            socket_obj = sockets.get(ref)
    if socket_obj is None:
        raise CommandError(
            "socket_not_found",
            "no %s socket %r on node %r (%s)" % (in_out.lower(), ref, node.name, node.bl_idname),
            details={"sockets": _socket_summary(sockets)},
        )
    if require_linked is True and not socket_obj.is_linked:
        raise CommandError(
            "socket_not_linked",
            "socket %r on node %r is not linked" % (socket_obj.identifier, node.name),
        )
    if require_linked is False and socket_obj.is_linked:
        raise CommandError(
            "socket_linked",
            "socket %r on node %r is linked; unlink it first" % (socket_obj.identifier, node.name),
        )
    return socket_obj


def _socket_summary(sockets, limit: int = 40):
    out = []
    for index, sock in enumerate(sockets):
        if index >= limit:
            break
        entry = {
            "index": index,
            "name": sock.name,
            "identifier": sock.identifier,
            "type": sock.type,
            "is_linked": bool(getattr(sock, "is_linked", False)),
        }
        default = _socket_default(sock)
        if default is not None:
            entry["default_value"] = default
        out.append(entry)
    return out


def _socket_default(sock):
    if not hasattr(sock, "default_value"):
        return None
    if sock.type in ("GEOMETRY", "SHADER", "VIRTUAL"):
        return None
    if _is_pointer_socket(sock):
        return _id_reference_payload(sock.default_value)
    try:
        return _jsonable(sock.default_value)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# datablock references ({"id_type": "OBJECT", "id_name": "Name"} / null)
# --------------------------------------------------------------------------- #
#: Reference kinds this server can resolve. Resolution always goes through an
#: explicit ``bpy.data`` collection - never through evaluation of a string.
REFERENCE_ID_TYPES = {
    "OBJECT": "objects",
    "NODETREE": "node_groups",
    "MATERIAL": "materials",
    "COLLECTION": "collections",
    "IMAGE": "images",
}
#: RNA class name expected for each reference kind (for fixed-type checking).
REFERENCE_RNA_TYPES = {
    "OBJECT": "Object",
    "NODETREE": "NodeTree",
    "MATERIAL": "Material",
    "COLLECTION": "Collection",
    "IMAGE": "Image",
}
_REFERENCE_ID_TYPE_BY_RNA = {value: key for key, value in REFERENCE_RNA_TYPES.items()}


def is_reference_value(value) -> bool:
    """True for the structured reference form used by pointer arguments."""
    return isinstance(value, dict) and "id_type" in value


def _id_reference_payload(block):
    """Serialise an ID datablock (or None) as a readable reference object."""
    if block is None:
        return None
    rna_name = getattr(getattr(block, "bl_rna", None), "identifier", None)
    id_type = _REFERENCE_ID_TYPE_BY_RNA.get(rna_name)
    if id_type is None:
        for candidate, collection_name in REFERENCE_ID_TYPES.items():
            collection = getattr(bpy.data, collection_name, None)
            if collection is not None and collection.get(getattr(block, "name", "")) is block:
                id_type = candidate
                break
    return {
        "id_type": id_type or rna_name or type(block).__name__,
        "id_name": getattr(block, "name", None),
        "rna_type": rna_name,
    }


def resolve_id_reference(value):
    """Resolve ``{"id_type": ..., "id_name": ...}`` to a datablock, or None to clear.

    Raises ``CommandError`` for unknown kinds, missing datablocks and malformed
    payloads. No string is ever evaluated as code.
    """
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise CommandError(
            "invalid_reference",
            "an ID reference must be an object like {\"id_type\": \"OBJECT\", \"id_name\": \"Name\"} "
            "(or null to clear the pointer)",
            details={"received": _jsonable(value), "supported": sorted(REFERENCE_ID_TYPES)},
        )
    id_type = value.get("id_type")
    if not isinstance(id_type, str) or not id_type.strip():
        raise CommandError(
            "invalid_reference",
            "reference needs a non-empty string 'id_type'",
            details={"received": _jsonable(value), "supported": sorted(REFERENCE_ID_TYPES)},
        )
    id_type = id_type.strip().upper()
    if id_type not in REFERENCE_ID_TYPES:
        raise CommandError(
            "invalid_reference",
            "unsupported reference id_type %r" % id_type,
            details={"supported": sorted(REFERENCE_ID_TYPES)},
        )
    id_name = value.get("id_name")
    if id_name is None or (isinstance(id_name, str) and not id_name.strip()):
        return id_type, None  # explicit clear
    if not isinstance(id_name, str):
        raise CommandError(
            "invalid_reference",
            "reference 'id_name' must be a string or null",
            details={"received": _jsonable(value)},
        )
    collection_name = REFERENCE_ID_TYPES[id_type]
    collection = getattr(bpy.data, collection_name, None)
    if collection is None:
        raise CommandError(
            "invalid_reference",
            "this Blender build has no bpy.data.%s collection" % collection_name,
        )
    block = collection.get(id_name)
    if block is None:
        raise CommandError(
            "reference_not_found",
            "no %s datablock named %r" % (id_type, id_name),
            details={"id_type": id_type, "id_name": id_name,
                     "available": [item.name for item in collection][:50]},
        )
    return id_type, block


def _pointer_rna_property(owner, property_name: str):
    """The RNA property object for a datablock pointer name, or None."""
    rna = getattr(owner, "bl_rna", None)
    if rna is None or property_name not in rna.properties:
        return None
    prop = rna.properties[property_name]
    return prop if getattr(prop, "type", None) == "POINTER" else None


def validate_reference_target(prop, id_type: str, block, label: str, node=None) -> None:
    """Check a resolved datablock against the RNA fixed type of its property."""
    fixed = getattr(prop, "fixed_type", None)
    expected = getattr(fixed, "identifier", None) if fixed is not None else None
    wanted = REFERENCE_RNA_TYPES[id_type]
    if expected and expected not in (wanted, "ID"):
        raise CommandError(
            "reference_type_mismatch",
            "%s expects a %s datablock, not %s (%s)"
            % (label, expected, id_type, getattr(block, "name", None)),
            details={"target": label, "expected_rna_type": expected, "given_id_type": id_type},
        )
    if wanted == "NodeTree" and node is not None and node.bl_idname == "GeometryNodeGroup":
        # Blender happily accepts a ShaderNodeTree here and produces a broken node,
        # so the group node type is validated explicitly.
        actual = getattr(getattr(block, "bl_rna", None), "identifier", None)
        if actual != "GeometryNodeTree":
            raise CommandError(
                "reference_type_mismatch",
                "a GeometryNodeGroup needs a GeometryNodeTree, got %s (%s)" % (actual, block.name),
                details={"target": label, "expected_rna_type": "GeometryNodeTree", "actual": actual},
            )


def _is_pointer_socket(sock) -> bool:
    rna = getattr(sock, "bl_rna", None)
    if rna is None or "default_value" not in rna.properties:
        return False
    return getattr(rna.properties["default_value"], "type", None) == "POINTER"


def set_pointer_socket_value(sock, value, node_label: str = ""):
    """Assign (or clear) a datablock pointer socket default."""
    where = node_label or getattr(sock, "name", "?")
    prop = sock.bl_rna.properties["default_value"]
    if value is not None and not is_reference_value(value):
        raise CommandError(
            "invalid_reference",
            "pointer socket %r on %s needs a reference object "
            "{\"id_type\": \"OBJECT\", \"id_name\": \"Name\"} or null, got %r"
            % (sock.identifier, where, _jsonable(value)),
            details={"socket": sock.identifier, "socket_type": sock.type,
                     "expected_rna_type": getattr(getattr(prop, "fixed_type", None), "identifier", None),
                     "supported": sorted(REFERENCE_ID_TYPES)},
        )
    id_type, block = resolve_id_reference(value)
    if block is not None:
        validate_reference_target(prop, id_type, block, "socket %r on %s" % (sock.identifier, where))
    try:
        setattr(sock, "default_value", block)
    except (TypeError, ValueError) as exc:
        raise CommandError(
            "invalid_reference",
            "Blender rejected %s for socket %r on %s: %s" % (id_type or "null", sock.identifier, where, exc),
        ) from exc
    return _id_reference_payload(sock.default_value)



def set_socket_value(sock, value, node_label: str = ""):
    """Assign a socket default value with coercion, then read the truth back."""
    where = node_label or getattr(sock, "name", "?")
    if not hasattr(sock, "default_value"):
        raise CommandError(
            "socket_not_settable",
            "socket %r (%s) on %s has no default_value" % (sock.identifier, sock.type, where),
        )
    if _is_pointer_socket(sock):
        # Datablock pointer sockets take the structured reference form (or null).
        return set_pointer_socket_value(sock, value, node_label=where)
    if value is None:
        raise CommandError("invalid_value", "value must not be null for socket %r" % sock.identifier)
    if is_reference_value(value):
        raise CommandError(
            "invalid_reference",
            "socket %r on %s is a %s socket, not a datablock pointer; "
            "references are only accepted by OBJECT/NODETREE/MATERIAL/COLLECTION/IMAGE pointers"
            % (sock.identifier, where, sock.type),
        )

    target = sock.default_value
    try:
        if isinstance(target, bool):
            setattr(sock, "default_value", bool(value))
        elif isinstance(target, float):
            setattr(sock, "default_value", float(value))
        elif isinstance(target, int):
            setattr(sock, "default_value", int(value))
        elif hasattr(target, "to_tuple"):
            size = len(target.to_tuple())
            seq = list(value) if isinstance(value, (list, tuple, set)) else [value]
            if size == 4 and len(seq) == 3:
                from mathutils import Euler  # noqa: PLC0415 - bundled with Blender

                quat = Euler([float(x) for x in seq], "XYZ").to_quaternion()
                setattr(sock, "default_value", quat)
            else:
                if len(seq) == 1:
                    seq = seq * size
                if len(seq) < size:
                    seq = seq + [0.0] * (size - len(seq))
                setattr(sock, "default_value", [float(x) for x in seq[:size]])
        elif isinstance(target, (list, tuple)) and target:
            size = len(target)
            seq = list(value) if isinstance(value, (list, tuple, set)) else [value]
            if len(seq) < size:
                seq = seq + [0.0] * (size - len(seq))
            setattr(sock, "default_value", [float(x) for x in seq[:size]])
        elif isinstance(target, str):
            setattr(sock, "default_value", str(value))
        else:
            setattr(sock, "default_value", value)
    except (TypeError, ValueError) as exc:
        raise CommandError(
            "invalid_value",
            "cannot assign %r to socket %r (%s): %s" % (value, sock.identifier, sock.type, exc),
        ) from exc
    return _socket_default(sock)


def _socket_types_compatible(a, b) -> bool:
    if a.type == b.type:
        return True
    if a.type in _NUMERIC_SOCKET_TYPES and b.type in _NUMERIC_SOCKET_TYPES:
        return True
    return (a.type, b.type) in _SOCKET_COMPATIBLE


def _serialize_property_value(owner, key: str, value):
    """Readback for a property: reference payload for datablock pointers."""
    prop = _pointer_rna_property(owner, key)
    if prop is not None:
        return _id_reference_payload(value)
    return _jsonable(value)


def apply_properties(owner, settings: dict, label: str = "") -> tuple[list, list]:
    """setattr each setting, validating against RNA. Returns (applied, failed)."""
    applied, failed = [], []
    for key, value in (settings or {}).items():
        try:
            if not hasattr(owner, key):
                raise AttributeError("no RNA property named %r" % key)
            prop = _pointer_rna_property(owner, key)
            if prop is not None:
                # Datablock pointer property (e.g. GeometryNodeGroup.node_tree).
                if value is not None and not is_reference_value(value):
                    raise ValueError(
                        "pointer property %r needs a reference object like "
                        "{\"id_type\": \"NODETREE\", \"id_name\": \"Name\"} or null" % key
                    )
                id_type, block = resolve_id_reference(value)
                if block is not None:
                    validate_reference_target(prop, id_type, block, "property %r on %s" % (key, label or owner), node=owner)
                setattr(owner, key, block)
                applied.append({"property": key, "reference": _id_reference_payload(getattr(owner, key))})
                continue
            if is_reference_value(value):
                raise ValueError(
                    "property %r is not a datablock pointer, so it does not accept a reference object" % key
                )
            current = getattr(owner, key)
            if isinstance(current, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value)
            setattr(owner, key, value)
            applied.append({"property": key, "value": _jsonable(getattr(owner, key))})
        except CommandError as exc:
            entry = {"property": key, "error": exc.message, "code": exc.code}
            if exc.details is not None:
                entry["details"] = _jsonable(exc.details)
            failed.append(entry)
        except (AttributeError, TypeError, ValueError) as exc:
            entry = {"property": key, "error": "%s: %s" % (type(exc).__name__, exc)}
            rna = getattr(owner, "bl_rna", None)
            if rna is not None and key in rna.properties:
                enum = getattr(rna.properties[key], "enum_items", None)
                if enum:
                    entry["allowed"] = [item.identifier for item in enum]
            failed.append(entry)
    if failed and label:
        for entry in failed:
            entry["owner"] = label
    return applied, failed


def depsgraph_update():
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    return deps


def flush_object(obj) -> None:
    """Tag an object so the next dependency-graph evaluation really rebuilds it.

    Verified in Blender 5.2.1: writing a Geometry Nodes modifier parameter through
    the ID-property based ``modifier.properties.inputs`` does *not* by itself
    invalidate the modifier.  ``depsgraph.update()`` and ``view_layer.update()``
    alone both return the stale mesh; ``object.update_tag()`` followed by
    ``depsgraph.update()`` returns the rebuilt mesh.
    """
    try:
        obj.update_tag()
    except Exception:  # noqa: BLE001
        pass


def flush_group_users(group) -> int:
    """Tag every object that consumes a node group, plus the group itself."""
    tagged = 0
    try:
        group.update_tag()
    except Exception:  # noqa: BLE001
        pass
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == "NODES" and _is_same_node_group(getattr(mod, "node_group", None), group):
                flush_object(obj)
                tagged += 1
    return tagged


def flush_owner(owner) -> None:
    """Tag an animation/driver owner and everything that depends on it.

    A driver on a node tree changes the geometry of every object that uses that
    tree as a modifier, and a driver on a Key changes the mesh of its object, so
    the dependents are tagged as well.
    """
    try:
        owner.update_tag()
    except Exception:  # noqa: BLE001
        pass
    try:
        if isinstance(owner, bpy.types.NodeTree):
            flush_group_users(owner)
        elif isinstance(owner, bpy.types.Key):
            for obj in bpy.data.objects:
                if obj.type == "MESH" and obj.data is not None and obj.data.shape_keys is owner:
                    flush_object(obj)
    except Exception:  # noqa: BLE001
        pass


def _bounds(points):
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return {
        "min": [round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
        "max": [round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)],
        "size": [round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6), round(max(zs) - min(zs), 6)],
    }


def evaluated_mesh_stats(obj, deps=None) -> dict:
    """Vertex/edge/polygon counts and world-space bounds of the evaluated mesh."""
    deps = deps or bpy.context.evaluated_depsgraph_get()
    stats = {
        "base_vertices": len(obj.data.vertices) if obj.type == "MESH" and obj.data else None,
        "base_edges": len(obj.data.edges) if obj.type == "MESH" and obj.data else None,
        "base_polygons": len(obj.data.polygons) if obj.type == "MESH" and obj.data else None,
        "evaluated": None,
        "bounds": None,
    }
    if obj.type != "MESH":
        return stats
    try:
        ev = obj.evaluated_get(deps)
        mesh = ev.to_mesh()
        try:
            matrix = ev.matrix_world
            points = [tuple(matrix @ v.co) for v in mesh.vertices]
            stats["evaluated"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "loops": len(mesh.loops),
                "name": mesh.name,
            }
            stats["bounds"] = _bounds(points)
            stats["bounds_source"] = "evaluated mesh vertices in world space"
        finally:
            ev.to_mesh_clear()
    except Exception as exc:  # noqa: BLE001
        stats["evaluated_error"] = "%s: %s" % (type(exc).__name__, exc)
    return stats


def mesh_signature(obj, deps=None) -> dict:
    """Small, comparable fingerprint of the evaluated mesh (for change assertions)."""
    deps = deps or bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(deps)
    mesh = ev.to_mesh()
    try:
        matrix = ev.matrix_world
        points = [tuple(matrix @ v.co) for v in mesh.vertices]
        rounded = [[round(c, 5) for c in p] for p in points]
        return {
            "vertices": len(mesh.vertices),
            "polygons": len(mesh.polygons),
            "bounds": _bounds(points),
            "checksum": hash(tuple(tuple(p) for p in rounded)) & 0xFFFFFFFF,
            "sample": rounded[:3],
        }
    finally:
        ev.to_mesh_clear()


# --------------------------------------------------------------------------- #
# bridge server
# --------------------------------------------------------------------------- #
def normalize_loopback_host(host) -> str | None:
    """Return the normalised loopback host, or None when it is not loopback.

    Accepts ``localhost`` (normalised to ``127.0.0.1``) and any literal IPv4
    loopback address (127.0.0.0/8, which covers 127.0.0.1) as well as ``::1``.
    Hostnames are *not* resolved: resolving could map to a non-loopback address.
    """
    if host is None:
        return None
    text = str(host).strip()
    if not text:
        return None
    if text.lower() in ("localhost", "localhost."):
        return "127.0.0.1"
    candidate = text
    if candidate.startswith("[") and candidate.endswith("]"):  # [::1]
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.version == 4 and address.is_loopback:
        return str(address)
    if address.version == 6 and address.is_loopback:
        return "::1"
    return None


class BridgeServer:
    """Non-blocking, single-threaded JSON bridge. Only ``poll`` is public."""

    def __init__(self, host: str, port: int, token: str, log, max_frame: int = MAX_FRAME_BYTES):
        self.host = host
        self.port = port
        self._token = token
        self.log = log
        self.max_frame = max_frame
        self.stop_requested = False
        self.started_at = None
        self.requests_ok = 0
        self.requests_failed = 0
        self.unauthorized = 0
        self.last_error = None
        self.last_command = None
        self.last_command_at = None
        self._sock: socket.socket | None = None
        self._clients: dict[socket.socket, dict] = {}
        self._polling = False
        self._round_robin = 0
        self._id_seq = 0

    # -- lifecycle -------------------------------------------------------- #
    @property
    def running(self) -> bool:
        return self._sock is not None

    @property
    def address(self) -> str:
        return "%s:%d" % (self.host, self.port)

    def start(self) -> None:
        if self.running:
            return
        # Loopback only: the bridge exposes scene mutation, so a non-loopback bind
        # is refused before any socket is created.
        normalized = normalize_loopback_host(self.host)
        if normalized is None:
            raise CommandError(
                "invalid_host",
                "the bridge only accepts loopback binds (127.0.0.1, localhost, ::1 or 127.0.0.0/8); "
                "refusing %r" % self.host,
                details={"host": self.host, "allowed": ["127.0.0.1", "localhost", "127.0.0.0/8", "::1"]},
            )
        self.host = normalized
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows: SO_REUSEADDR allows a *second* process to bind the same port
        # and silently steal traffic.  SO_EXCLUSIVEADDRUSE makes the second bind
        # fail loudly instead, which is what we want for a single-owner bridge.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise CommandError(
                "bind_failed",
                "cannot bind %s: %s" % (self.address, exc),
                details={"host": self.host, "port": self.port},
            ) from exc
        sock.listen(max(8, MAX_CLIENTS * 4))
        sock.setblocking(False)
        self._sock = sock
        self.started_at = time.time()
        self.log("bridge listening on %s (protocol v%d)" % (self.address, PROTOCOL_VERSION))

    def stop(self, reason: str = "requested") -> None:
        for conn in list(self._clients):
            self._close(conn, "server stopping")
        self._clients.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.log("bridge stopped (%s)" % reason)

    # -- internals -------------------------------------------------------- #
    def _close(self, conn: socket.socket, reason: str = "") -> None:
        state = self._clients.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass
        if reason and state is not None and state.get("requests"):
            self.log("connection closed (%s)" % reason)

    def _queue(self, conn: socket.socket, payload: dict) -> None:
        state = self._clients.get(conn)
        if state is None:
            return
        try:
            data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            data = (
                json.dumps(
                    {
                        "id": payload.get("id"),
                        "ok": False,
                        "error": {"code": "serialization_failed", "message": str(exc)},
                    }
                )
                + "\n"
            ).encode("utf-8")
        if len(state["out"]) + len(data) > MAX_OUT_BUFFER_BYTES:
            self._close(conn, "output buffer overflow; client is not reading")
            return
        state["out"] += data

    def _flush(self, conn: socket.socket) -> None:
        state = self._clients.get(conn)
        if state is None or not state["out"]:
            return
        try:
            sent = conn.send(state["out"])
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._close(conn, "send failed: %s" % exc)
            return
        state["out"] = state["out"][sent:]
        if state.get("close_after_flush") and not state["out"]:
            self._begin_linger(conn, state)

    def _begin_linger(self, conn: socket.socket, state: dict) -> None:
        """Graceful close: half-close, then drain so our answer is not RST away."""
        state["linger"] = LINGER_MAX_POLLS
        state["linger_bytes"] = LINGER_MAX_BYTES
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            self._close(conn, "closing after error response")
            return
        if LINGER_MAX_POLLS <= 0 or LINGER_MAX_BYTES <= 0:
            self._close(conn, "closing after error response")

    def _drain_lingering(self) -> None:
        """Bounded discard of a lingering peer's remaining bytes, then close."""
        for conn in list(self._clients):
            state = self._clients.get(conn)
            if state is None or "linger" not in state:
                continue
            if state["linger"] <= 0 or state["linger_bytes"] <= 0:
                self._close(conn, "closing after error response")
                continue
            try:
                chunk = conn.recv(min(4096, state["linger_bytes"]))
            except (BlockingIOError, InterruptedError):
                state["linger"] -= 1
                if state["linger"] <= 0:
                    self._close(conn, "closing after error response")
                continue
            except OSError:
                self._close(conn, "closing after error response")
                continue
            if not chunk:
                self._close(conn, "peer closed after error response")
                continue
            state["linger_bytes"] -= len(chunk)

    def _reject(self, conn: socket.socket, request_id, code: str, message: str) -> None:
        """Answer with an explicit error, flushing it before the socket closes."""
        self._queue(conn, {"id": request_id, "ok": False, "error": {"code": code, "message": message}})
        state = self._clients.get(conn)
        if state is None:
            return
        state["close_after_flush"] = True
        self._flush(conn)

    def poll(self) -> None:
        """One non-blocking pass: accept, read, dispatch, write. Never blocks."""
        if not self.running or self._polling:
            return
        self._polling = True
        try:
            self._accept_ready()
            self._read_ready()
            for conn in list(self._clients):
                self._flush(conn)
            self._drain_lingering()
        finally:
            self._polling = False

    def _accept_ready(self) -> None:
        accepted = 0
        while accepted < MAX_ACCEPTS_PER_POLL:
            try:
                conn, peer = self._sock.accept()  # type: ignore[union-attr]
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self.log("accept failed: %s" % exc)
                return
            accepted += 1
            conn.setblocking(False)
            if len(self._clients) >= MAX_CLIENTS:
                # Refuse loudly instead of silently queueing an unbounded backlog.
                # The listen backlog is deliberately larger than MAX_CLIENTS so this
                # message is reachable instead of the OS refusing the TCP handshake.
                try:
                    conn.setblocking(True)
                    conn.settimeout(1.0)
                    conn.sendall(
                        (
                            json.dumps(
                                {
                                    "id": None,
                                    "ok": False,
                                    "error": {
                                        "code": "too_many_clients",
                                        "message": "bridge already serves %d clients" % MAX_CLIENTS,
                                    },
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
                self.log("refused a connection: client limit %d reached" % MAX_CLIENTS)
                continue
            self._id_seq += 1
            self._clients[conn] = {
                "id": self._id_seq,
                "peer": peer,
                "in": b"",
                "out": b"",
                "requests": 0,
                "authed": False,
            }

    def _drain_lines(self, conn: socket.socket, state: dict, limit: int) -> int:
        """Process at most ``limit`` complete queued frames. Leftovers stay queued."""
        handled = 0
        while handled < limit and b"\n" in state["in"]:
            current = self._clients.get(conn)
            if current is None or current is not state or state.get("close_after_flush") \
                    or "linger" in state:
                break
            line, _, rest = state["in"].partition(b"\n")
            state["in"] = rest
            self._handle_line(conn, line)
            handled += 1
        return handled

    def _read_ready(self) -> None:
        """One bounded read/dispatch pass, round-robin over the clients.

        Guarantees that matter on Blender's main thread:
          * at most MAX_MESSAGES_PER_POLL frames are executed per poll in total,
            so a burst of pipelined requests cannot freeze the UI;
          * no single client is served more than MAX_MESSAGES_PER_POLL_PER_CLIENT
            frames per poll and no more than MAX_BYTES_PER_POLL_PER_CLIENT bytes
            is read from it, so one client cannot starve the others;
          * complete frames already buffered are always processed first, so a
            pipelined burst still drains on later polls even when no new bytes
            arrive;
          * the rotation cursor moves each poll, so a client that always sends
            work does not permanently win over a quiet one.
        """
        conns = list(self._clients)
        if not conns:
            return
        start = self._round_robin % len(conns)
        ordered = conns[start:] + conns[:start]
        self._round_robin = (start + 1) % len(conns)
        message_budget = MAX_MESSAGES_PER_POLL
        for conn in ordered:
            if message_budget <= 0:
                return
            state = self._clients.get(conn)
            if state is None or "linger" in state:
                continue
            # 1) frames that are already complete (queued by an earlier poll).
            client_budget = min(message_budget, MAX_MESSAGES_PER_POLL_PER_CLIENT)
            handled = self._drain_lines(conn, state, client_budget)
            message_budget -= handled
            client_budget -= handled
            state = self._clients.get(conn)
            if state is None or message_budget <= 0 or client_budget <= 0 or state.get("close_after_flush"):
                continue
            # 2) read a bounded number of bytes, then drain again.
            bytes_budget = MAX_BYTES_PER_POLL_PER_CLIENT
            over_limit = False
            while bytes_budget > 0:
                try:
                    chunk = conn.recv(min(65536, bytes_budget))
                except (BlockingIOError, InterruptedError):
                    break
                except ConnectionResetError:
                    self._close(conn, "reset by peer")
                    over_limit = True
                    break
                except OSError as exc:
                    self._close(conn, "recv failed: %s" % exc)
                    over_limit = True
                    break
                if not chunk:
                    self._close(conn, "peer closed")
                    over_limit = True
                    break
                bytes_budget -= len(chunk)
                state["in"] += chunk
                if len(state["in"]) > self.max_frame and b"\n" not in state["in"]:
                    # Refuse the frame explicitly instead of buffering without bound.
                    self._reject(
                        conn, None, "frame_too_large", "request frame exceeds %d bytes" % self.max_frame
                    )
                    state["in"] = b""
                    over_limit = True
                    break
                if b"\n" in state["in"]:
                    break  # process what we have before reading more
            if over_limit or conn not in self._clients:
                continue
            state = self._clients.get(conn)
            if state is None or state.get("close_after_flush"):
                continue
            message_budget -= self._drain_lines(conn, state, min(message_budget, client_budget))

    def _handle_line(self, conn: socket.socket, line: bytes) -> None:
        state = self._clients.get(conn)
        if state is None:
            return
        request_id = None
        if len(line) > self.max_frame:
            self._reject(conn, None, "frame_too_large", "request frame exceeds %d bytes" % self.max_frame)
            return
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._reject(conn, None, "bad_request", "request is not a UTF-8 JSON line: %s" % exc)
            return
        if not isinstance(request, dict):
            self._reject(conn, None, "bad_request", "request must be a JSON object")
            return

        request_id = request.get("id")
        token = request.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, self._token):
            self.unauthorized += 1
            state["requests"] += 1
            self._reject(
                conn,
                request_id,
                "unauthorized",
                "invalid or missing bridge token (see MCP/.runtime/token)",
            )
            return

        state["authed"] = True
        state["requests"] += 1
        command_name = request.get("command")
        params = request.get("params") or {}
        if not isinstance(command_name, str) or not command_name:
            self._reject(conn, request_id, "bad_request", "'command' must be a non-empty string")
            return
        if not isinstance(params, dict):
            self._reject(conn, request_id, "bad_request", "'params' must be an object")
            return

        self.last_command = command_name
        self.last_command_at = time.time()
        try:
            result = dispatch(command_name, params)
            self.requests_ok += 1
            self._queue(
                conn,
                {
                    "id": request_id,
                    "ok": True,
                    "result": _jsonable(result),
                    "protocol": PROTOCOL_VERSION,
                },
            )
        except CommandError as exc:
            self.requests_failed += 1
            self.last_error = "%s: %s" % (exc.code, exc.message)
            self._queue(conn, {"id": request_id, "ok": False, "error": exc.to_payload()})
        except Exception as exc:  # noqa: BLE001 - convert every crash to a protocol error
            self.requests_failed += 1
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.log("command %r crashed:\n%s" % (command_name, traceback.format_exc()))
            self._queue(
                conn,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "%s: %s" % (type(exc).__name__, exc),
                        "details": {"command": command_name},
                    },
                },
            )

    def status(self) -> dict:
        return {
            "running": self.running,
            "address": self.address,
            "host": self.host,
            "port": self.port,
            "clients": len(self._clients),
            "requests_ok": self.requests_ok,
            "requests_failed": self.requests_failed,
            "unauthorized_requests": self.unauthorized,
            "last_command": self.last_command,
            "last_error": self.last_error,
            "uptime_s": round(time.time() - self.started_at, 3) if self.started_at else None,
            "token_file": str(default_token_path()),
        }


# --------------------------------------------------------------------------- #
# command registry
# --------------------------------------------------------------------------- #
_COMMANDS: dict[str, dict] = {}


def command(name: str, mutating: bool = True, summary: str = ""):
    def decorator(func):
        _COMMANDS[name] = {"func": func, "mutating": mutating, "summary": summary or (func.__doc__ or "").strip()}
        return func

    return decorator


def dispatch(name: str, params: dict):
    entry = _COMMANDS.get(name)
    if entry is None:
        raise CommandError(
            "unknown_command",
            "unknown bridge command %r" % name,
            details={"available": sorted(_COMMANDS)},
        )
    func = entry["func"]
    signature = inspect.signature(func)
    accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())
    if not accepts_kwargs:
        accepted = [p.name for p in signature.parameters.values()]
        unknown = sorted(set(params) - set(accepted))
        if unknown:
            raise CommandError(
                "invalid_argument",
                "unknown parameter(s) for command %r: %s" % (name, ", ".join(unknown)),
                details={"accepted": accepted},
            )
        missing = [
            p.name
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and p.name not in params
        ]
        if missing:
            raise CommandError(
                "invalid_argument",
                "missing required parameter(s) for command %r: %s" % (name, ", ".join(missing)),
                details={"accepted": accepted},
            )
    return func(**params)


# --------------------------------------------------------------------------- #
# commands: meta
# --------------------------------------------------------------------------- #
@command("ping", mutating=False, summary="liveness probe")
def _cmd_ping() -> dict:
    return {"pong": True, "protocol": PROTOCOL_VERSION}


@command("health", mutating=False, summary="bridge + Blender version and status")
def _cmd_health():
    scene = bpy.context.scene
    return {
        "bridge": "blender-mcp",
        "addon_version": ADDON_VERSION,
        "protocol": PROTOCOL_VERSION,
        "blender_version": bpy.app.version_string,
        "blender_version_tuple": list(bpy.app.version),
        "python_version": sys.version.split()[0],
        "pid": os.getpid(),
        "background": bool(bpy.app.background),
        "factory_startup": bool(bpy.app.factory_startup),
        "blend_filepath": bpy.data.filepath,
        "scene": scene.name if scene else None,
        "objects": len(bpy.data.objects),
        "node_groups": len(bpy.data.node_groups),
        "commands": sorted(_COMMANDS),
        "server": _SERVER.status() if _SERVER else None,
        "token_file": str(default_token_path()),
        "project_dir": str(project_dir()),
        "time": time.time(),
    }


@command("server_status", mutating=False, summary="bridge server counters")
def _cmd_server_status():
    if _SERVER is None:
        return {"running": False, "reason": "bridge server not started in this Blender session"}
    return _SERVER.status()


@command("shutdown", mutating=True, summary="ask the bridge to stop (host decides how to exit)")
def _cmd_shutdown():
    if _SERVER is None:
        return {"stopping": False}
    _SERVER.stop_requested = True
    _SERVER.log("shutdown requested by client")
    return {"stopping": True}


@command("list_commands", mutating=False, summary="available bridge commands")
def _cmd_list_commands():
    return {
        "protocol": PROTOCOL_VERSION,
        "commands": [
            {"name": name, "mutating": entry["mutating"], "summary": entry["summary"]}
            for name, entry in sorted(_COMMANDS.items())
        ],
    }


# --------------------------------------------------------------------------- #
# commands: scene / object introspection
# --------------------------------------------------------------------------- #
def _object_row(obj) -> dict:
    modifiers = [m.type for m in obj.modifiers]
    return {
        "name": obj.name,
        "type": obj.type,
        "data": getattr(obj.data, "name", None),
        "vertices": len(obj.data.vertices) if obj.type == "MESH" and obj.data else None,
        "polygons": len(obj.data.polygons) if obj.type == "MESH" and obj.data else None,
        "modifiers": modifiers,
        "has_geometry_nodes": any(m.type == "NODES" for m in obj.modifiers),
        "shape_keys": (
            len(obj.data.shape_keys.key_blocks)
            if obj.type == "MESH" and obj.data and obj.data.shape_keys
            else 0
        ),
        "drivers": len(obj.animation_data.drivers) if obj.animation_data else 0,
        "custom_properties": sorted(k for k in obj.keys() if k != "_RNA_UI"),
    }


@command("scene_summary", mutating=False, summary="scene, collections and object overview")
def _cmd_scene_summary(include_objects: bool = True):
    scene = bpy.context.scene
    deps = bpy.context.evaluated_depsgraph_get()
    payload = {
        "scene": scene.name,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "unit_system": scene.unit_settings.system,
        "blend_filepath": bpy.data.filepath,
        "objects_total": len(bpy.data.objects),
        "collections": [c.name for c in bpy.data.collections],
        "materials": len(bpy.data.materials),
        "node_groups": [
            {
                "name": g.name,
                "type": g.bl_rna.identifier,
                "nodes": len(g.nodes),
                "users": g.users,
            }
            for g in bpy.data.node_groups
        ],
        "depsgraph_objects": len(deps.object_instances) if hasattr(deps, "object_instances") else None,
    }
    if include_objects:
        payload["objects"] = [_object_row(o) for o in bpy.context.scene.objects]
    return payload


@command("list_objects", mutating=False, summary="list objects, optionally filtered")
def _cmd_list_objects(name_contains: str | None = None, type: str | None = None, limit: int = 200):
    rows = []
    for obj in bpy.data.objects:
        if type and obj.type != type.upper():
            continue
        if name_contains and name_contains.lower() not in obj.name.lower():
            continue
        rows.append(_object_row(obj))
    return {"count": len(rows), "truncated": len(rows) > limit, "objects": rows[: max(0, int(limit))]}


@command("object_info", mutating=False, summary="deep object introspection with evaluated mesh data")
def _cmd_object_info(name: str, include_evaluated: bool = True):
    obj = get_object(name)
    deps = bpy.context.evaluated_depsgraph_get()
    info = _object_row(obj)
    info["location"] = [round(v, 6) for v in obj.location]
    info["rotation_euler"] = [round(v, 6) for v in obj.rotation_euler]
    info["scale"] = [round(v, 6) for v in obj.scale]
    info["matrix_world"] = [[round(c, 6) for c in row] for row in obj.matrix_world]
    info["custom_properties"] = {
        k: _jsonable(v) for k, v in obj.items() if k != "_RNA_UI"
    }
    info["modifiers"] = [_modifier_info(obj, m, deps) for m in obj.modifiers]
    info["vertex_groups"] = [g.name for g in obj.vertex_groups]
    info["materials"] = [slot.material.name if slot.material else None for slot in obj.material_slots]
    if obj.type == "MESH" and obj.data:
        info["shape_keys"] = _shape_key_report(obj)
    if include_evaluated:
        info["mesh"] = evaluated_mesh_stats(obj, deps)
        if obj.type == "MESH":
            info["evaluated_signature"] = mesh_signature(obj, deps)
    info["has_animation_data"] = obj.animation_data is not None
    info["drivers"] = _driver_report(obj)
    return info


def _modifier_info(obj, mod, deps=None) -> dict:
    entry = {
        "name": mod.name,
        "type": mod.type,
        "show_viewport": bool(mod.show_viewport),
        "show_render": bool(mod.show_render),
    }
    if mod.type == "NODES":
        group = getattr(mod, "node_group", None)
        entry["node_group"] = group.name if group else None
        entry["is_geometry_nodes"] = bool(group and group.bl_rna.identifier == "GeometryNodeTree")
        entry["parameters"] = _modifier_parameters(mod, group)
    elif mod.type == "SUBSURF":
        entry["levels"] = mod.levels
    return entry


def _modifier_parameters(mod, group=None) -> dict:
    """Per-instance geometry-nodes input values (Blender 5.x IDProperty storage)."""
    out = {}
    props = getattr(mod, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    if inputs is None:
        return out
    names = {}
    kinds = {}
    if group is not None:
        try:
            for item in group.interface.items_tree:
                if item.in_out == "INPUT":
                    names[item.identifier] = item.name
                    kinds[item.identifier] = item.socket_type
        except Exception:  # noqa: BLE001
            pass
    try:
        keys = list(inputs.keys())
    except Exception:  # noqa: BLE001
        return out
    pointer_types = ("NodeSocketObject", "NodeSocketMaterial", "NodeSocketCollection", "NodeSocketImage")
    for key in keys:
        item = inputs[key]
        value = None
        try:
            value = item["value"] if "value" in item.keys() else None
        except Exception:  # noqa: BLE001
            value = None
        entry = {"name": names.get(key, key)}
        if kinds.get(key) in pointer_types:
            # Pointer parameters read back as a reference object (or null).
            entry["reference"] = _id_reference_payload(value) if value is not None and not isinstance(
                value, (int, float, str, bool, list)
            ) else None
            entry["value"] = entry["reference"]
        else:
            entry["value"] = _jsonable(value)
        out[key] = entry
    return out


def _shape_key_report(obj) -> dict:
    mesh = obj.data
    keys = mesh.shape_keys.key_blocks if mesh.shape_keys else []
    return {
        "has_shape_keys": bool(mesh.shape_keys),
        "use_relative": bool(mesh.shape_keys.use_relative) if mesh.shape_keys else None,
        "active_index": obj.active_shape_key_index,
        "active_key": obj.active_shape_key.name if obj.active_shape_key else None,
        "basis": keys[0].name if keys else None,
        "keys": [
            {
                "name": k.name,
                "index": i,
                "value": round(float(k.value), 6),
                "mute": bool(k.mute),
                "slider_min": k.slider_min,
                "slider_max": k.slider_max,
                "relative_key": k.relative_key.name if k.relative_key else None,
                "vertices": len(k.data),
            }
            for i, k in enumerate(keys)
        ],
        "mesh_vertices": len(mesh.vertices),
        "topology_consistent": all(len(k.data) == len(mesh.vertices) for k in keys),
    }


# --------------------------------------------------------------------------- #
# commands: mesh creation / custom properties / save
# --------------------------------------------------------------------------- #
@command("create_mesh_object", summary="create a mesh object from explicit geometry")
def _cmd_create_mesh_object(
    name: str,
    vertices: list,
    faces: list | None = None,
    edges: list | None = None,
    collection: str | None = None,
    location: list | None = None,
):
    if not name:
        raise CommandError("invalid_argument", "name must not be empty")
    if not vertices:
        raise CommandError("invalid_argument", "vertices must be a non-empty list of [x, y, z]")
    if bpy.data.objects.get(name) is not None:
        raise CommandError(
            "already_exists",
            "an object named %r already exists; choose another name" % name,
        )
    verts = []
    for index, v in enumerate(vertices):
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            raise CommandError(
                "invalid_argument",
                "vertex %d must be [x, y, z], got %r" % (index, v),
            )
        verts.append(tuple(float(c) for c in v))
    face_list = []
    for index, f in enumerate(faces or []):
        if not isinstance(f, (list, tuple)) or len(f) < 3:
            raise CommandError("invalid_argument", "face %d must list at least 3 vertex indices" % index)
        idx = [int(i) for i in f]
        bad = [i for i in idx if i < 0 or i >= len(verts)]
        if bad:
            raise CommandError(
                "invalid_argument",
                "face %d references out-of-range vertex indices %s (mesh has %d vertices)"
                % (index, bad, len(verts)),
            )
        face_list.append(tuple(idx))
    edge_list = []
    for index, e in enumerate(edges or []):
        if not isinstance(e, (list, tuple)) or len(e) != 2:
            raise CommandError("invalid_argument", "edge %d must be [a, b]" % index)
        a, b = int(e[0]), int(e[1])
        if not (0 <= a < len(verts) and 0 <= b < len(verts)):
            raise CommandError("invalid_argument", "edge %d references out-of-range vertices" % index)
        edge_list.append((a, b))

    mesh = bpy.data.meshes.new(name)
    try:
        mesh.from_pydata(verts, edge_list, face_list)
        mesh.validate(verbose=False)
        mesh.update()
    except Exception as exc:  # noqa: BLE001
        bpy.data.meshes.remove(mesh)
        raise CommandError("mesh_creation_failed", str(exc)) from exc

    obj = bpy.data.objects.new(name, mesh)
    target = bpy.context.scene.collection
    if collection:
        coll = bpy.data.collections.get(collection)
        if coll is None:
            bpy.data.objects.remove(obj)
            bpy.data.meshes.remove(mesh)
            raise CommandError(
                "collection_not_found",
                "no collection named %r" % collection,
                details={"available": [c.name for c in bpy.data.collections]},
            )
        target = coll
    target.objects.link(obj)
    if location:
        obj.location = [float(c) for c in location]
    bpy.context.view_layer.update()
    return {
        "created": obj.name,
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "polygons": len(mesh.polygons),
        "collection": target.name,
        "location": [round(v, 6) for v in obj.location],
    }


_PRIMITIVES = {
    "cube": ("primitive_cube_add", {"size": 2.0}),
    "plane": ("primitive_plane_add", {"size": 2.0}),
    "uv_sphere": ("primitive_uv_sphere_add", {"segments": 32, "ring_count": 16, "radius": 1.0}),
    "ico_sphere": ("primitive_ico_sphere_add", {"subdivisions": 2, "radius": 1.0}),
    "cylinder": ("primitive_cylinder_add", {"vertices": 32, "radius": 1.0, "depth": 2.0}),
    "cone": ("primitive_cone_add", {"vertices": 32, "radius1": 1.0, "radius2": 0.0, "depth": 2.0}),
    "torus": ("primitive_torus_add", {"major_segments": 48, "minor_segments": 12, "major_radius": 1.0, "minor_radius": 0.25}),
    "grid": ("primitive_grid_add", {"x_subdivisions": 10, "y_subdivisions": 10, "size": 2.0}),
    "monkey": ("primitive_monkey_add", {"size": 2.0}),
}


@command("create_primitive", summary="create a primitive mesh object via bpy.ops")
def _cmd_create_primitive(
    kind: str,
    name: str | None = None,
    location: list | None = None,
    rotation: list | None = None,
    scale: list | None = None,
    size: float | None = None,
    radius: float | None = None,
    parameters: dict | None = None,
):
    key = str(kind).lower()
    if key not in _PRIMITIVES:
        raise CommandError(
            "invalid_argument",
            "unknown primitive %r" % kind,
            details={"supported": sorted(_PRIMITIVES)},
        )
    op_name, defaults = _PRIMITIVES[key]
    kwargs = dict(defaults)
    kwargs.update(parameters or {})
    if size is not None and "size" in kwargs:
        kwargs["size"] = float(size)
    if radius is not None:
        if "radius" in kwargs:
            kwargs["radius"] = float(radius)
        if "radius1" in kwargs:
            kwargs["radius1"] = float(radius)
    if location:
        kwargs["location"] = tuple(float(c) for c in location)
    if rotation:
        kwargs["rotation"] = tuple(float(c) for c in rotation)
    if scale:
        kwargs["scale"] = tuple(float(c) for c in scale)
    op = getattr(bpy.ops.mesh, op_name)
    try:
        op(**kwargs)
    except (TypeError, RuntimeError) as exc:
        raise CommandError(
            "primitive_failed",
            "%s failed: %s" % (op_name, exc),
            details={"kwargs": _jsonable(kwargs)},
        ) from exc
    obj = bpy.context.active_object or bpy.context.object
    if obj is None:
        raise CommandError("primitive_failed", "%s produced no active object" % op_name)
    if name:
        obj.name = name
        if obj.data:
            obj.data.name = name
    bpy.context.view_layer.update()
    return {
        "created": obj.name,
        "kind": key,
        "vertices": len(obj.data.vertices),
        "polygons": len(obj.data.polygons),
        "location": [round(v, 6) for v in obj.location],
        "parameters_used": _jsonable(kwargs),
    }


def _owner_for_custom_property(owner_kind: str, owner_name: str | None):
    kind = (owner_kind or "object").lower()
    if kind == "object":
        if not owner_name:
            raise CommandError("invalid_argument", "owner_name is required for owner_kind='object'")
        return get_object(owner_name)
    if kind == "scene":
        scene = bpy.context.scene
        if owner_name and bpy.data.scenes.get(owner_name) is not None:
            scene = bpy.data.scenes[owner_name]
        return scene
    raise CommandError(
        "invalid_argument",
        "owner_kind must be 'object' or 'scene', got %r" % owner_kind,
    )


@command("set_custom_property", summary="set/get/remove an ID custom property on an object or scene")
def _cmd_set_custom_property(
    owner_kind: str,
    key: str,
    value=None,
    owner_name: str | None = None,
    remove: bool = False,
):
    if not key:
        raise CommandError("invalid_argument", "key must not be empty")
    owner = _owner_for_custom_property(owner_kind, owner_name)
    label = "%s:%s" % (owner_kind, getattr(owner, "name", "?"))
    if remove:
        if key not in owner.keys():
            raise CommandError("not_found", "property %r does not exist on %s" % (key, label))
        del owner[key]
        return {"owner": label, "key": key, "removed": True, "properties": sorted(k for k in owner.keys() if k != "_RNA_UI")}
    if value is None:
        raise CommandError("invalid_argument", "value is required unless remove=True")
    try:
        if isinstance(value, list):
            owner[key] = [float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in value]
        else:
            owner[key] = value
    except (TypeError, ValueError) as exc:
        raise CommandError("invalid_value", "cannot set %r: %s" % (key, exc)) from exc
    owner.update_tag()
    depsgraph_update()
    driven = _driven_warning(owner, '["%s"]' % key)
    warnings = []
    if driven:
        warnings.append(
            "custom property %r has a driver (%s); the driven value wins on the next evaluation"
            % (key, driven["expressions"])
        )
    return {
        "owner": label,
        "key": key,
        "value": _jsonable(owner[key]),
        "type": type(owner[key]).__name__,
        "driven": bool(driven),
        "driver": driven,
        "warnings": warnings,
        "properties": sorted(k for k in owner.keys() if k != "_RNA_UI"),
    }


@command("save_blend", summary="save the .blend inside <workspace>/Project (overwrite is opt-in)")
def _cmd_save_blend(relative_path: str, overwrite: bool = False, compress: bool = False):
    target = resolve_project_path(relative_path)
    if target.suffix.lower() != ".blend":
        raise CommandError(
            "invalid_path",
            "only .blend files may be saved here, got %r" % target.name,
            details={"path": str(target)},
        )
    if target.exists() and not overwrite:
        raise CommandError(
            "file_exists",
            "refusing to overwrite existing file without overwrite=true",
            details={"path": str(target), "relative_path": relative_path},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Blender would otherwise also write a preview thumbnail into the operating
    # system's thumbnail cache (outside the workspace); this tool keeps every
    # write inside <workspace>/Project.
    filepaths = getattr(bpy.context.preferences, "filepaths", None)
    previous_preview = getattr(filepaths, "use_save_preview_images", None)
    previous_versions = getattr(filepaths, "save_version", None)
    try:
        if previous_preview is not None:
            filepaths.use_save_preview_images = False
        if previous_versions is not None:
            # Do not scatter .blend1/.blend2 backups next to the requested file.
            filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(
            filepath=str(target),
            check_existing=False,
            compress=bool(compress),
            copy=False,
        )
    except RuntimeError as exc:
        raise CommandError("save_failed", "save_as_mainfile failed: %s" % exc) from exc
    finally:
        if previous_preview is not None:
            try:
                filepaths.use_save_preview_images = previous_preview
            except Exception:  # noqa: BLE001
                pass
        if previous_versions is not None:
            try:
                filepaths.save_version = previous_versions
            except Exception:  # noqa: BLE001
                pass
    if not target.exists():
        raise CommandError("save_failed", "Blender reported success but %s does not exist" % target)
    return {
        "saved": True,
        "path": str(target),
        "relative_path": os.path.relpath(str(target), str(project_dir())).replace(os.sep, "/"),
        "bytes": target.stat().st_size,
        "overwritten": bool(overwrite),
        "blend_filepath": bpy.data.filepath,
    }


@command("set_frame", summary="set the scene frame (drivers re-evaluate)")
def _cmd_set_frame(frame: int, subframe: float = 0.0):
    scene = bpy.context.scene
    scene.frame_set(int(frame), subframe=float(subframe))
    depsgraph_update()
    return {
        "frame_current": scene.frame_current,
        "frame_float": scene.frame_float,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
    }


@command("update_depsgraph", mutating=False, summary="force a dependency-graph update")
def _cmd_update_depsgraph():
    deps = depsgraph_update()
    return {"updated": True, "frame_current": bpy.context.scene.frame_current, "objects": len(bpy.data.objects)}


# --------------------------------------------------------------------------- #
# commands: geometry nodes
# --------------------------------------------------------------------------- #
_INTERFACE_SOCKET_TYPES = (
    "NodeSocketFloat",
    "NodeSocketInt",
    "NodeSocketBool",
    "NodeSocketVector",
    "NodeSocketColor",
    "NodeSocketRotation",
    "NodeSocketString",
    "NodeSocketMenu",
    "NodeSocketGeometry",
    "NodeSocketMaterial",
    "NodeSocketObject",
    "NodeSocketCollection",
    "NodeSocketImage",
)


def _interface_item_default_payload(item):
    """Interface default readback: reference payload for pointer sockets."""
    prop = getattr(item, "bl_rna", None)
    prop = prop.properties.get("default_value") if prop is not None else None
    if prop is not None and getattr(prop, "type", None) == "POINTER":
        return _id_reference_payload(getattr(item, "default_value", None))
    return _jsonable(getattr(item, "default_value", None))


def set_interface_item_default(item, value, index: int, socket_type: str):
    """Set one interface socket default, handling datablock pointers."""
    prop = item.bl_rna.properties.get("default_value")
    is_pointer = prop is not None and getattr(prop, "type", None) == "POINTER"
    if is_pointer:
        if value is not None and not is_reference_value(value):
            raise CommandError(
                "invalid_reference",
                "interface[%d] default_value for %s needs a reference object "
                "{\"id_type\": \"OBJECT\", \"id_name\": \"Name\"} or null, got %r"
                % (index, socket_type, _jsonable(value)),
                details={"supported": sorted(REFERENCE_ID_TYPES)},
            )
        id_type, block = resolve_id_reference(value)
        if block is not None:
            validate_reference_target(prop, id_type, block, "interface[%d] (%s)" % (index, item.name))
        try:
            item.default_value = block
        except (TypeError, ValueError) as exc:
            raise CommandError(
                "invalid_value",
                "interface[%d] default_value %r rejected: %s" % (index, value, exc),
            ) from exc
        return
    try:
        item.default_value = value
    except (TypeError, ValueError) as exc:
        raise CommandError(
            "invalid_value",
            "interface[%d] default_value %r rejected for %s: %s" % (index, value, socket_type, exc),
        ) from exc


def _interface_payload(group) -> dict:
    items = []
    for item in group.interface.items_tree:
        entry = {
            "name": item.name,
            "identifier": item.identifier,
            "in_out": item.in_out,
            "socket_type": item.socket_type,
            "item_type": getattr(item, "item_type", None),
            "description": getattr(item, "description", ""),
        }
        for attr in ("default_value", "min_value", "max_value", "subtype", "hide_value", "hide_in_modifier",
                     "default_attribute_name", "structure_type", "optional_label"):
            if hasattr(item, attr):
                if attr == "default_value":
                    entry[attr] = _interface_item_default_payload(item)
                else:
                    entry[attr] = _jsonable(getattr(item, attr))
        items.append(entry)
    return {
        "items": items,
        "inputs": [i for i in items if i["in_out"] == "INPUT"],
        "outputs": [i for i in items if i["in_out"] == "OUTPUT"],
        "panels": [
            {
                "name": p.name,
                "identifier": getattr(p, "identifier", None),
                "in_out": getattr(p, "in_out", None),
                "parent": getattr(getattr(p, "parent", None), "name", None),
            }
            for p in group.interface.items_tree
            if getattr(p, "item_type", "") == "PANEL"
        ],
    }


def _node_payload(node) -> dict:
    entry = {
        "name": node.name,
        "index": list(node.id_data.nodes).index(node) if node.id_data else None,
        "bl_idname": node.bl_idname,
        "bl_label": getattr(node, "bl_label", ""),
        "label": node.label,
        "mute": bool(node.mute),
        "location": [round(float(c), 3) for c in node.location],
        "parent": node.parent.name if node.parent else None,
        "internal_links": len(getattr(node, "internal_links", [])),
    }
    if node.bl_idname in ("NodeGroupInput", "NodeGroupOutput"):
        entry["role"] = "group_interface"
    if hasattr(node, "node_tree") and node.node_tree is not None:
        entry["node_tree"] = node.node_tree.name
        entry["node_tree_type"] = node.node_tree.bl_rna.identifier
    entry["inputs"] = [
        {
            "index": i,
            "name": s.name,
            "identifier": s.identifier,
            "type": s.type,
            "is_linked": bool(getattr(s, "is_linked", False)),
            **({"default_value": _socket_default(s)} if _socket_default(s) is not None else {}),
        }
        for i, s in enumerate(node.inputs)
    ]
    entry["outputs"] = [
        {
            "index": i,
            "name": s.name,
            "identifier": s.identifier,
            "type": s.type,
            "is_linked": bool(getattr(s, "is_linked", False)),
            **({"default_value": _socket_default(s)} if _socket_default(s) is not None else {}),
        }
        for i, s in enumerate(node.outputs)
    ]
    return entry


def _group_modifier_users(group) -> list:
    users = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == "NODES" and _is_same_node_group(getattr(mod, "node_group", None), group):
                users.append({"object": obj.name, "modifier": mod.name})
    return users


def _inspect_group(group, include_sockets: bool = True) -> dict:
    payload = {
        "name": group.name,
        "type": group.bl_rna.identifier,
        "is_geometry_tree": group.bl_rna.identifier == "GeometryNodeTree",
        "users": group.users,
        "nodes": [],
        "links": [
            {
                "from_node": link.from_node.name,
                "from_socket": link.from_socket.identifier,
                "from_socket_name": link.from_socket.name,
                "to_node": link.to_node.name,
                "to_socket": link.to_socket.identifier,
                "to_socket_name": link.to_socket.name,
                "is_valid": bool(getattr(link, "is_valid", True)),
                "is_muted": bool(getattr(link, "is_muted", False)),
            }
            for link in group.links
        ],
        "interface": _interface_payload(group),
        "modifier_users": _group_modifier_users(group),
        "drivers": _driver_report(group),
        "frame_range": list(group.frame_range) if hasattr(group, "frame_range") else None,
    }
    for node in group.nodes:
        entry = _node_payload(node)
        if not include_sockets:
            entry.pop("inputs", None)
            entry.pop("outputs", None)
        payload["nodes"].append(entry)
    return payload


@command("inspect_node_group", mutating=False, summary="nodes, indices, sockets, identifiers, links, interface")
def _cmd_inspect_node_group(name: str, include_sockets: bool = True):
    group = get_node_group(name)
    return _inspect_group(group, include_sockets=include_sockets)


@command("create_node_group", summary="create a GeometryNodeTree with typed interface sockets")
def _cmd_create_node_group(
    name: str,
    interface: list | None = None,
    with_input_output: bool = True,
    geometry_output: bool = True,
    if_exists: str = "error",
):
    if not name:
        raise CommandError("invalid_argument", "name must not be empty")
    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        if if_exists == "reuse":
            return {"created": False, "reused": True, "group": _inspect_group(existing)}
        if if_exists != "replace":
            raise CommandError(
                "already_exists",
                "a node group named %r already exists (use if_exists='reuse' or 'replace')" % name,
                details={"existing_type": existing.bl_rna.identifier, "users": existing.users},
            )
        if existing.users > 0:
            raise CommandError(
                "in_use",
                "node group %r has %d user(s); refusing to replace it" % (name, existing.users),
            )
        bpy.data.node_groups.remove(existing)

    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    declared_outputs = []
    declared_inputs = []
    for index, spec in enumerate(interface or []):
        if not isinstance(spec, dict):
            bpy.data.node_groups.remove(group)
            raise CommandError("invalid_argument", "interface[%d] must be an object" % index)
        socket_name = spec.get("name")
        socket_type = spec.get("socket_type") or spec.get("type")
        in_out = str(spec.get("in_out", "INPUT")).upper()
        if not socket_name or not socket_type:
            bpy.data.node_groups.remove(group)
            raise CommandError(
                "invalid_argument",
                "interface[%d] needs 'name' and 'socket_type'" % index,
                details={"supported_socket_types": list(_INTERFACE_SOCKET_TYPES)},
            )
        if socket_type not in _INTERFACE_SOCKET_TYPES:
            bpy.data.node_groups.remove(group)
            raise CommandError(
                "invalid_socket_type",
                "interface[%d] socket_type %r is not a known interface socket type" % (index, socket_type),
                details={"supported_socket_types": list(_INTERFACE_SOCKET_TYPES)},
            )
        if in_out not in ("INPUT", "OUTPUT"):
            bpy.data.node_groups.remove(group)
            raise CommandError("invalid_argument", "interface[%d].in_out must be INPUT or OUTPUT" % index)
        try:
            item = group.interface.new_socket(socket_name, in_out=in_out, socket_type=socket_type)
        except Exception as exc:  # noqa: BLE001
            bpy.data.node_groups.remove(group)
            raise CommandError(
                "interface_failed",
                "cannot create interface socket %r (%s): %s" % (socket_name, socket_type, exc),
            ) from exc
        for attr in ("description", "subtype", "default_attribute_name"):
            if spec.get(attr) is not None and hasattr(item, attr):
                setattr(item, attr, spec[attr])
        for attr in ("min_value", "max_value"):
            raw = spec.get(attr)
            if raw is None or not hasattr(item, attr):
                continue
            try:
                setattr(item, attr, raw)
            except TypeError:
                # Integer sockets (NodeSocketInt / NodeSocketMenu) reject floats.
                current = getattr(item, attr)
                try:
                    setattr(item, attr, type(current)(raw))
                except (TypeError, ValueError) as exc:
                    bpy.data.node_groups.remove(group)
                    raise CommandError(
                        "invalid_value",
                        "interface[%d].%s=%r rejected for %s: %s" % (index, attr, raw, socket_type, exc),
                    ) from exc
        for attr in ("hide_value", "hide_in_modifier"):
            if spec.get(attr) is not None and hasattr(item, attr):
                setattr(item, attr, bool(spec[attr]))
        if "default_value" in spec and hasattr(item, "default_value"):
            try:
                set_interface_item_default(item, spec["default_value"], index, socket_type)
            except CommandError as exc:
                bpy.data.node_groups.remove(group)
                raise
            except Exception as exc:  # noqa: BLE001
                bpy.data.node_groups.remove(group)
                raise CommandError(
                    "invalid_value",
                    "interface[%d] default_value %r rejected for %s: %s"
                    % (index, spec.get("default_value"), socket_type, exc),
                ) from exc
        if in_out == "OUTPUT":
            declared_outputs.append(item)
        else:
            declared_inputs.append(item)

    if with_input_output:
        group.nodes.new("NodeGroupInput")
        group.nodes.new("NodeGroupOutput")
        if geometry_output and not any(i.socket_type == "NodeSocketGeometry" for i in declared_outputs):
            group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    flush_group_users(group)
    return {"created": True, "reused": False, "group": _inspect_group(group)}


@command("create_node", summary="add a node to a node group")
def _cmd_create_node(
    group: str,
    node_type: str,
    name: str | None = None,
    location: list | None = None,
    label: str | None = None,
    settings: dict | None = None,
):
    tree = get_node_group(group)
    try:
        node = tree.nodes.new(str(node_type))
    except RuntimeError as exc:
        raise CommandError(
            "invalid_node_type",
            "cannot create node of type %r: %s" % (node_type, exc),
            details={"hint": "use list_node_types / describe_node_type to discover valid types"},
        ) from exc
    except TypeError as exc:
        raise CommandError("invalid_node_type", "node type %r is not creatable: %s" % (node_type, exc)) from exc
    if name:
        node.name = str(name)
    if label is not None:
        node.label = str(label)
    if location:
        node.location = (float(location[0]), float(location[1]))
    applied, failed = apply_properties(node, settings or {}, label=node.name)
    if failed:
        # Keep a failed create_node atomic: the freshly created node is removed again.
        try:
            tree.nodes.remove(node)
        except Exception:  # noqa: BLE001
            pass
        flush_group_users(tree)
        raise CommandError(
            "invalid_settings",
            "settings were rejected; the node was not kept",
            details={"applied": applied, "failed": failed, "node_type": node_type},
        )
    flush_group_users(tree)
    return {"group": tree.name, "node": _node_payload(node), "settings_applied": applied}


@command("set_node_property", summary="set an RNA property on a node")
def _cmd_set_node_property(group: str, node, property: str, value):
    tree = get_node_group(group)
    target = get_node(tree, node)
    # Changing a node's group pointer rebuilds its sockets, so any existing link on
    # that node is dropped by Blender. Report it instead of losing it silently.
    before_links = len([link for link in tree.links if _link_touches_node(link, target)])
    applied, failed = apply_properties(target, {property: value}, label=target.name)
    if failed:
        first = failed[0]
        if first.get("code"):
            # Surface the precise reference error (type mismatch / not found) instead
            # of a generic wrapper; the pointer was left untouched.
            raise CommandError(
                first["code"],
                first.get("error") or "cannot set %r on node %r" % (property, target.name),
                details={"node": target.name, "node_type": target.bl_idname, "property": property,
                         "failed": failed, "value_unchanged": True},
            )
        raise CommandError(
            "invalid_property",
            "cannot set %r on node %r (%s)" % (property, target.name, target.bl_idname),
            details={"failed": failed},
        )
    flush_group_users(tree)
    after_links = len([link for link in tree.links if _link_touches_node(link, target)])
    warnings = []
    if after_links < before_links:
        warnings.append(
            "changing %r rebuilt the node's sockets and Blender dropped %d link(s); re-link them "
            "afterwards (inspect the node for the new socket identifiers)"
            % (property, before_links - after_links)
        )
    return {
        "node": target.name,
        "property": property,
        "value": _serialize_property_value(target, property, getattr(target, property)),
        "applied": applied,
        "links_before": before_links,
        "links_after": after_links,
        "warnings": warnings,
    }


@command("set_socket_default", summary="set a node socket's default value (identifier, name or index)")
def _cmd_set_socket_default(group: str, node, socket, value, in_out: str = "INPUT"):
    tree = get_node_group(group)
    target = get_node(tree, node)
    sock = get_socket(target, socket, in_out)
    readback = set_socket_value(sock, value, node_label="%s.%s" % (target.name, target.bl_idname))
    flush_group_users(tree)
    return {
        "group": tree.name,
        "node": target.name,
        "socket_identifier": sock.identifier,
        "socket_name": sock.name,
        "socket_type": sock.type,
        "value": readback,
        "note": "default_value on a linked socket is ignored by the node graph until unlinked",
    }


@command("link_sockets", summary="link an output socket to an input socket")
def _cmd_link_sockets(group: str, from_node, from_socket, to_node, to_socket, replace: bool = True):
    tree = get_node_group(group)
    src_node = get_node(tree, from_node)
    dst_node = get_node(tree, to_node)
    src = get_socket(src_node, from_socket, "OUTPUT")
    dst = get_socket(dst_node, to_socket, "INPUT")
    if not _socket_types_compatible(src, dst):
        raise CommandError(
            "incompatible_sockets",
            "cannot link %s (%s) to %s (%s)"
            % (src.identifier, src.type, dst.identifier, dst.type),
            details={
                "from": {"node": src_node.name, "socket": src.identifier, "type": src.type},
                "to": {"node": dst_node.name, "socket": dst.identifier, "type": dst.type},
            },
        )
    if dst.is_linked and not replace:
        raise CommandError(
            "socket_linked",
            "input socket %r on %r is already linked (pass replace=true to relink)"
            % (dst.identifier, dst_node.name),
        )
    try:
        link = tree.links.new(src, dst)
    except (RuntimeError, TypeError) as exc:
        raise CommandError("link_failed", "Blender rejected the link: %s" % exc) from exc
    flush_group_users(tree)
    return {
        "group": tree.name,
        "link": {
            "from_node": link.from_node.name,
            "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name,
            "to_socket": link.to_socket.identifier,
        },
        "is_valid": bool(getattr(link, "is_valid", True)),
    }


@command("unlink_socket", summary="remove links touching one socket")
def _cmd_unlink_socket(group: str, node, socket, in_out: str = "INPUT"):
    tree = get_node_group(group)
    target = get_node(tree, node)
    sock = get_socket(target, socket, in_out)
    links = [link for link in tree.links if _link_touches_socket(link, target, sock, in_out)]
    removed = [
        {
            "from_node": l.from_node.name,
            "from_socket": l.from_socket.identifier,
            "to_node": l.to_node.name,
            "to_socket": l.to_socket.identifier,
        }
        for l in links
    ]
    for link in links:
        tree.links.remove(link)
    flush_group_users(tree)
    return {"group": tree.name, "removed": removed, "removed_count": len(removed)}


@command("remove_node", summary="remove a node from a node group")
def _cmd_remove_node(group: str, node):
    tree = get_node_group(group)
    target = get_node(tree, node)
    name = target.name
    bl_idname = target.bl_idname
    if bl_idname in ("NodeGroupInput", "NodeGroupOutput"):
        raise CommandError(
            "protected_node",
            "refusing to remove the group interface node %r (%s)" % (name, bl_idname),
        )
    incoming = len([link for link in tree.links if link.to_node.name == target.name])
    outgoing = len([link for link in tree.links if link.from_node.name == target.name])
    tree.nodes.remove(target)
    flush_group_users(tree)
    return {"group": tree.name, "removed": name, "bl_idname": bl_idname, "links_removed": incoming + outgoing}


@command("attach_geometry_nodes_modifier", summary="attach a node group as a Geometry Nodes modifier")
def _cmd_attach_gn_modifier(
    object: str,
    group: str,
    modifier_name: str | None = None,
    parameters: dict | None = None,
    reuse_existing: bool = True,
):
    obj = get_mesh_object(object)
    tree = get_node_group(group)
    if tree.bl_rna.identifier != "GeometryNodeTree":
        raise CommandError(
            "wrong_node_group_type",
            "node group %r is %s, not a GeometryNodeTree" % (tree.name, tree.bl_rna.identifier),
        )
    existing = obj.modifiers.get(modifier_name or tree.name)
    if existing is not None and existing.type == "NODES" and _is_same_node_group(existing.node_group, tree) and reuse_existing:
        mod = existing
        created = False
    else:
        if existing is not None and not reuse_existing:
            raise CommandError(
                "already_exists",
                "object %r already has a modifier named %r" % (obj.name, existing.name),
            )
        mod = obj.modifiers.new(modifier_name or tree.name, "NODES")
        created = True
    mod.node_group = tree
    mod.show_viewport = True
    mod.show_render = True
    applied, failed = _set_modifier_parameters(obj, mod, tree, parameters or {})
    deps = depsgraph_update()
    return {
        "object": obj.name,
        "modifier": mod.name,
        "created": created,
        "node_group": tree.name,
        "parameters_applied": applied,
        "parameters_failed": failed,
        "parameters": _modifier_parameters(mod, tree),
        "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
        "evaluated": evaluated_mesh_stats(obj, deps),
    }


def _set_modifier_parameters(obj, mod, group, parameters: dict) -> tuple[list, list]:
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is None:
        if parameters:
            raise CommandError(
                "unsupported_blender_version",
                "this Blender build exposes no per-instance geometry-nodes inputs",
                details={"blender": bpy.app.version_string},
            )
        return [], []
    by_name, by_identifier = {}, {}
    for item in group.interface.items_tree:
        if item.in_out != "INPUT":
            continue
        by_name[item.name] = item
        by_identifier[item.identifier] = item
    available = [{"name": item.name, "identifier": item.identifier} for item in by_name.values()]
    applied, failed = [], []
    for key, value in parameters.items():
        item = by_name.get(key) or by_identifier.get(key)
        if item is None:
            failed.append({"input": key, "error": "no exposed input with this name/identifier", "available": available})
            continue
        if item.identifier not in inputs.keys():
            failed.append(
                {
                    "input": key,
                    "identifier": item.identifier,
                    "error": "input is not settable on this modifier instance (linked or hidden)",
                    "available": sorted(inputs.keys()),
                }
            )
            continue
        socket_type = item.socket_type
        try:
            if socket_type == "NodeSocketGeometry":
                failed.append({"input": key, "error": "%s inputs cannot be set by value" % socket_type})
                continue
            if socket_type in ("NodeSocketMaterial", "NodeSocketObject",
                               "NodeSocketCollection", "NodeSocketImage"):
                # Datablock-pointer parameters: only the structured reference form
                # (or null) is accepted, because the ID-property store happily takes
                # a plain string that would not name any datablock.
                if value is not None and not is_reference_value(value):
                    failed.append(
                        {
                            "input": key,
                            "identifier": item.identifier,
                            "error": "invalid_reference",
                            "hint": "pointer inputs need {\"id_type\": \"OBJECT\", \"id_name\": \"Name\"} or null",
                            "supported": sorted(REFERENCE_ID_TYPES),
                        }
                    )
                    continue
                id_type, block = resolve_id_reference(value)
                prop = item.bl_rna.properties.get("default_value")
                if block is not None and prop is not None:
                    validate_reference_target(prop, id_type, block, "modifier input %r" % key)
                inputs[item.identifier]["value"] = block
                back = inputs[item.identifier]["value"] if "value" in inputs[item.identifier].keys() else None
                applied.append(
                    {"input": key, "identifier": item.identifier,
                     "reference": _id_reference_payload(back)}
                )
                continue
            if socket_type == "NodeSocketBundle" and not isinstance(value, (int, float)):
                failed.append({"input": key, "error": "%s inputs are not supported by this server" % socket_type})
                continue
            coerced = value
            if socket_type == "NodeSocketInt":
                coerced = int(value)
            elif socket_type == "NodeSocketFloat":
                coerced = float(value)
            elif socket_type == "NodeSocketBool":
                coerced = bool(value)
            elif socket_type in ("NodeSocketVector", "NodeSocketColor", "NodeSocketRotation"):
                seq = list(value) if isinstance(value, (list, tuple)) else [value]
                coerced = [float(x) for x in seq]
            elif socket_type in ("NodeSocketString", "NodeSocketMenu"):
                coerced = str(value)
            inputs[item.identifier]["value"] = coerced
            back = inputs[item.identifier]["value"] if "value" in inputs[item.identifier].keys() else None
            applied.append({"input": key, "identifier": item.identifier, "value": _jsonable(back)})
        except CommandError as exc:
            failed.append(
                {"input": key, "identifier": item.identifier, "error": exc.code,
                 "message": exc.message, "details": _jsonable(exc.details) if exc.details else None}
            )
        except (TypeError, ValueError, KeyError) as exc:
            failed.append(
                {"input": key, "identifier": item.identifier, "error": "%s: %s" % (type(exc).__name__, exc)}
            )
    if applied:
        # ID-property writes on the modifier do not invalidate the modifier by
        # themselves; the owning object must be tagged for the graph to rebuild.
        flush_object(obj)
    return applied, failed


@command("set_modifier_input", summary="set one exposed Geometry Nodes modifier parameter")
def _cmd_set_modifier_input(
    object: str,
    input: str,
    value,
    modifier: str | None = None,
    include_evaluated: bool = False,
):
    obj = get_mesh_object(object)
    mods = [m for m in obj.modifiers if m.type == "NODES"]
    if modifier:
        mod = obj.modifiers.get(modifier)
        if mod is None or mod.type != "NODES":
            raise CommandError(
                "modifier_not_found",
                "object %r has no Geometry Nodes modifier named %r" % (obj.name, modifier),
                details={"geometry_nodes_modifiers": [m.name for m in mods]},
            )
    else:
        if not mods:
            raise CommandError(
                "modifier_not_found",
                "object %r has no Geometry Nodes modifier" % obj.name,
            )
        if len(mods) > 1:
            raise CommandError(
                "ambiguous_modifier",
                "object %r has %d Geometry Nodes modifiers; pass modifier=" % (obj.name, len(mods)),
                details={"geometry_nodes_modifiers": [m.name for m in mods]},
            )
        mod = mods[0]
    group = mod.node_group
    if group is None:
        raise CommandError("no_node_group", "modifier %r has no node group" % mod.name)
    applied, failed = _set_modifier_parameters(obj, mod, group, {input: value})
    if failed:
        raise CommandError(
            "invalid_modifier_input",
            "cannot set input %r on %s.%s" % (input, obj.name, mod.name),
            details={"failed": failed},
        )
    deps = depsgraph_update()
    payload = {
        "object": obj.name,
        "modifier": mod.name,
        "applied": applied,
        "parameters": _modifier_parameters(mod, group),
    }
    if include_evaluated:
        payload["evaluated"] = evaluated_mesh_stats(obj, deps)
        payload["signature"] = mesh_signature(obj, deps)
    return payload


@command("list_node_types", mutating=False, summary="runtime discovery of creatable node types")
def _cmd_list_node_types(prefix: str = "GeometryNode", contains: str | None = None, limit: int = 200):
    limit = max(1, min(int(limit), 2000))
    names = []
    for attr in dir(bpy.types):
        if not attr.startswith(prefix):
            continue
        if contains and contains.lower() not in attr.lower():
            continue
        cls = getattr(bpy.types, attr)
        rna = getattr(cls, "bl_rna", None)
        if rna is None or rna.identifier != attr:
            continue
        props = {p.identifier for p in rna.properties}
        if "bl_idname" not in props or "inputs" not in props:
            continue
        names.append(attr)
    names.sort()
    return {
        "prefix": prefix,
        "contains": contains,
        "count": len(names),
        "truncated": len(names) > limit,
        "types": names[:limit],
        "known_prefixes": ["GeometryNode", "ShaderNode", "CompositorNode", "FunctionNode"],
        "note": "list comes from bpy.types at runtime; node availability depends on the running Blender build",
    }


@command("describe_node_type", mutating=False, summary="RNA properties and the real sockets of a node type")
def _cmd_describe_node_type(node_type: str):
    scratch_name = "__blender_mcp_describe__"
    scratch = bpy.data.node_groups.get(scratch_name)
    created_group = False
    if scratch is None:
        scratch = bpy.data.node_groups.new(scratch_name, "GeometryNodeTree")
        created_group = True
    node = None
    try:
        try:
            node = scratch.nodes.new(str(node_type))
        except (RuntimeError, TypeError) as exc:
            raise CommandError(
                "invalid_node_type",
                "cannot instantiate node type %r: %s" % (node_type, exc),
            ) from exc
        rna = node.bl_rna
        skip = {
            "rna_type", "inputs", "outputs", "internal_links", "parent", "dimensions",
            "location_absolute", "bl_rna", "name", "label", "location", "width", "height",
            "select", "show_options", "show_preview", "show_texture", "hide", "mute",
            "color", "use_custom_color", "color_tag", "panel_states", "warning_propagation",
            "show_preview", "bl_static_type",
        }
        props = []
        for prop in rna.properties:
            if prop.identifier in skip or prop.identifier.startswith("bl_"):
                continue
            entry = {
                "identifier": prop.identifier,
                "type": prop.type,
                "name": prop.name,
                "description": prop.description,
                "is_readonly": bool(getattr(prop, "is_readonly", False)),
            }
            if prop.type in ("FLOAT", "INT"):
                for attr in ("default", "hard_min", "hard_max", "soft_min", "soft_max", "subtype", "unit"):
                    if hasattr(prop, attr):
                        entry[attr] = _jsonable(getattr(prop, attr))
            elif prop.type in ("BOOLEAN", "STRING", "ENUM"):
                if hasattr(prop, "default"):
                    entry["default"] = _jsonable(prop.default)
            if prop.type == "ENUM":
                entry["enum_items"] = [item.identifier for item in prop.enum_items]
            props.append(entry)
        return {
            "node_type": node.bl_idname,
            "bl_label": getattr(node, "bl_label", "") or node.name,
            "description": getattr(node, "bl_description", ""),
            "inputs": _socket_summary(node.inputs, limit=200),
            "outputs": _socket_summary(node.outputs, limit=200),
            "properties": props,
            "socket_types_seen": sorted({s.type for s in list(node.inputs) + list(node.outputs)}),
        }
    finally:
        if node is not None:
            try:
                scratch.nodes.remove(node)
            except Exception:  # noqa: BLE001
                pass
        if created_group and not scratch.nodes:
            bpy.data.node_groups.remove(scratch)


@command("batch_graph_edit", summary="apply an ordered list of graph edits, reporting per-op results")
def _cmd_batch_graph_edit(group: str, operations: list, continue_on_error: bool = False):
    if not isinstance(operations, list) or not operations:
        raise CommandError("invalid_argument", "operations must be a non-empty list")
    tree = get_node_group(group)
    completed, failures = [], []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            entry = {"index": index, "op": None, "ok": False, "error": "operation must be an object"}
            failures.append(entry)
            if not continue_on_error:
                break
            continue
        op = operation.get("op")
        op_params = {k: v for k, v in operation.items() if k != "op"}
        try:
            result = _BATCH_OPS[op](tree, op_params) if op in _BATCH_OPS else None
            if result is None:
                raise CommandError(
                    "unknown_operation",
                    "unknown batch op %r" % op,
                    details={"supported": sorted(_BATCH_OPS)},
                )
            completed.append({"index": index, "op": op, "ok": True, "result": _jsonable(result)})
        except CommandError as exc:
            entry = {"index": index, "op": op, "ok": False, "error": exc.message, "code": exc.code}
            if exc.details is not None:
                entry["details"] = _jsonable(exc.details)
            failures.append(entry)
            if not continue_on_error:
                break
        except Exception as exc:  # noqa: BLE001
            failures.append({"index": index, "op": op, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            if not continue_on_error:
                break
    flush_group_users(tree)
    depsgraph_update()
    payload = {
        "group": tree.name,
        "completed": completed,
        "failures": failures,
        "completed_count": len(completed),
        "failed_count": len(failures),
        "partial": bool(failures) and bool(completed),
        "rollback": False,
        "note": (
            "operations are applied in order and are NOT transactional: edits listed in "
            "'completed' are already part of the node group and are not rolled back"
        ),
        "group_state": _inspect_group(tree, include_sockets=False),
    }
    return payload


def _batch_create_node(tree, params):
    return _cmd_create_node(group=tree.name, **params)


def _batch_set_socket_default(tree, params):
    return _cmd_set_socket_default(group=tree.name, **params)


def _batch_link_sockets(tree, params):
    return _cmd_link_sockets(group=tree.name, **params)


def _batch_unlink_socket(tree, params):
    return _cmd_unlink_socket(group=tree.name, **params)


def _batch_remove_node(tree, params):
    return _cmd_remove_node(group=tree.name, **params)


def _batch_set_node_property(tree, params):
    return _cmd_set_node_property(group=tree.name, **params)


_BATCH_OPS = {
    "create_node": _batch_create_node,
    "set_node_property": _batch_set_node_property,
    "set_socket_default": _batch_set_socket_default,
    "link_sockets": _batch_link_sockets,
    "unlink_socket": _batch_unlink_socket,
    "remove_node": _batch_remove_node,
}


# --------------------------------------------------------------------------- #
# commands: shape keys
# --------------------------------------------------------------------------- #
def _key_blocks(obj):
    mesh = obj.data
    if mesh.shape_keys is None:
        return []
    return list(mesh.shape_keys.key_blocks)


def _get_key_block(obj, name: str):
    blocks = _key_blocks(obj)
    for block in blocks:
        if block.name == name:
            return block
    raise CommandError(
        "shape_key_not_found",
        "object %r has no shape key named %r" % (obj.name, name),
        details={"keys": [k.name for k in blocks]},
    )


def _validate_shape_key_topology(obj, action: str) -> None:
    mesh = obj.data
    if len(mesh.vertices) == 0:
        raise CommandError(
            "topology_error",
            "%s needs a mesh with vertices; %r has an empty mesh" % (action, obj.name),
            details={
                "object": obj.name,
                "mesh_vertices": 0,
                "note": "shape keys deform the object's ORIGINAL mesh vertices; geometry generated by modifiers is unaffected",
            },
        )
    blocks = _key_blocks(obj)
    mismatch = [{"key": k.name, "vertices": len(k.data)} for k in blocks if len(k.data) != len(mesh.vertices)]
    if mismatch:
        raise CommandError(
            "topology_mismatch",
            "shape key data length no longer matches mesh topology on %r" % obj.name,
            details={"mesh_vertices": len(mesh.vertices), "mismatched": mismatch},
        )


@command("inspect_shape_keys", mutating=False, summary="shape key inventory + topology check")
def _cmd_inspect_shape_keys(object: str):
    obj = get_mesh_object(object)
    report = _shape_key_report(obj)
    report["object"] = obj.name
    report["modifiers"] = [{"name": m.name, "type": m.type} for m in obj.modifiers]
    report["has_geometry_nodes"] = any(m.type == "NODES" for m in obj.modifiers)
    report["topology_note"] = (
        "shape keys act on the original mesh vertex order; a Geometry Nodes modifier that "
        "generates new geometry replaces those vertices, so shape key influence is not visible "
        "in the evaluated result of such an object"
    )
    return report


@command("create_shape_key", summary="add a shape key (Basis is created automatically)")
def _cmd_create_shape_key(
    object: str,
    name: str,
    value: float = 0.0,
    from_mix: bool = False,
    slider_min: float | None = None,
    slider_max: float | None = None,
    set_active: bool = False,
):
    obj = get_mesh_object(object)
    if not name:
        raise CommandError("invalid_argument", "name must not be empty")
    _validate_shape_key_topology(obj, "creating a shape key")
    if obj.data.shape_keys is not None and name in obj.data.shape_keys.key_blocks:
        raise CommandError(
            "already_exists",
            "object %r already has a shape key named %r" % (obj.name, name),
            details={"keys": [k.name for k in obj.data.shape_keys.key_blocks]},
        )
    had_keys = obj.data.shape_keys is not None
    basis_created = False
    try:
        if not had_keys and str(name) != "Basis":
            # Blender 5.x names the *first* key whatever you ask for, and that key
            # silently becomes the reference.  Create the conventional 'Basis'
            # first so the requested key is a real, relative morph target.
            obj.shape_key_add(name="Basis", from_mix=False)
            basis_created = True
        block = obj.shape_key_add(name=str(name), from_mix=bool(from_mix))
        if not had_keys and str(name) == "Basis":
            basis_created = True
    except RuntimeError as exc:
        raise CommandError("shape_key_failed", "shape_key_add failed: %s" % exc) from exc
    block.value = float(value)
    if slider_min is not None:
        block.slider_min = float(slider_min)
    if slider_max is not None:
        block.slider_max = float(slider_max)
    if set_active:
        obj.active_shape_key_index = len(obj.data.shape_keys.key_blocks) - 1
    flush_object(obj)
    depsgraph_update()
    report = _shape_key_report(obj)
    report["object"] = obj.name
    report["created"] = block.name
    report["basis_created"] = basis_created
    report["vertex_count"] = len(block.data)
    return report


@command("set_shape_key_coordinates", summary="write key coordinates or additive deltas (topology checked)")
def _cmd_set_shape_key_coordinates(
    object: str,
    key_name: str,
    coordinates: list | None = None,
    deltas: list | None = None,
    indices: list | None = None,
    allow_basis: bool = False,
):
    obj = get_mesh_object(object)
    _validate_shape_key_topology(obj, "editing shape key coordinates")
    block = _get_key_block(obj, key_name)
    is_basis = obj.data.shape_keys.key_blocks[0] is block or block.name == "Basis"
    if is_basis and not allow_basis:
        raise CommandError(
            "basis_protected",
            "refusing to modify the Basis shape key (%s); pass allow_basis=true to force it "
            "(Basis defines the base mesh and every other key is relative to it)"
            % ("coordinates" if coordinates is not None else "deltas"),
            details={"key": block.name, "vertices": len(block.data),
                     "hint": "edit a non-basis key, or pass allow_basis=true"},
        )
    if (coordinates is None) == (deltas is None):
        raise CommandError(
            "invalid_argument",
            "provide exactly one of 'coordinates' (absolute) or 'deltas' (added to current values)",
        )
    payload = coordinates if coordinates is not None else deltas
    if not isinstance(payload, list) or not payload:
        raise CommandError("invalid_argument", "coordinates/deltas must be a non-empty list of [x, y, z]")

    total = len(block.data)
    if indices is None:
        if len(payload) != total:
            raise CommandError(
                "topology_mismatch",
                "got %d coordinate entries but shape key %r has %d vertices; shape keys require the "
                "object's original mesh topology and cannot be applied to a different vertex count"
                % (len(payload), block.name, total),
                details={
                    "object": obj.name,
                    "key": block.name,
                    "expected_vertices": total,
                    "received": len(payload),
                    "mesh_vertices": len(obj.data.vertices),
                },
            )
        targets = list(range(total))
    else:
        if not isinstance(indices, list) or len(indices) != len(payload):
            raise CommandError(
                "invalid_argument",
                "indices must be a list with the same length as coordinates/deltas",
                details={"indices": len(indices) if isinstance(indices, list) else None, "values": len(payload)},
            )
        targets = []
        for i in indices:
            if not isinstance(i, int) or i < 0 or i >= total:
                raise CommandError(
                    "invalid_argument",
                    "index %r is out of range for shape key %r (%d vertices)" % (i, block.name, total),
                )
            targets.append(i)

    # Validate the WHOLE payload before writing anything, so a malformed entry in
    # the middle cannot leave a partially written shape key behind.  Non-finite
    # values are refused here as well: NaN/Infinity would silently poison the key
    # data and every later evaluation.
    validated = []
    for slot in range(len(payload)):
        value = payload[slot]
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise CommandError(
                "invalid_argument",
                "entry %d must be [x, y, z], got %r (nothing was written)" % (slot, value),
            )
        try:
            components = (float(value[0]), float(value[1]), float(value[2]))
        except (TypeError, ValueError) as exc:
            raise CommandError(
                "invalid_argument",
                "entry %d has non-numeric components: %r (nothing was written)" % (slot, value),
            ) from exc
        if not all(math.isfinite(component) for component in components):
            raise CommandError(
                "non_finite_value",
                "entry %d contains NaN or Infinity (%r); non-finite coordinates are rejected before any "
                "vertex is written" % (slot, value),
                details={"object": obj.name, "key": block.name, "index": slot,
                         "values": [repr(component) for component in components]},
            )
        validated.append(components)

    changed = 0
    for slot, vertex_index in enumerate(targets):
        point = block.data[vertex_index]
        if deltas is not None:
            current = point.co
            new_co = (
                current[0] + validated[slot][0],
                current[1] + validated[slot][1],
                current[2] + validated[slot][2],
            )
        else:
            new_co = validated[slot]
        # Assign the whole vector: reading ``.co`` yields a wrapper whose
        # component-wise writes are not persisted by Blender's RNA here.
        point.co = new_co
        changed += 1

    obj.data.update_tag()
    obj.update_tag()
    if hasattr(block.data, "update"):
        block.data.update()
    deps = depsgraph_update()
    return {
        "object": obj.name,
        "key": block.name,
        "changed": changed,
        "mode": "coordinates" if coordinates is not None else "deltas",
        "key_vertices": total,
        "sample": [
            {"index": i, "co": [round(c, 6) for c in block.data[i].co]} for i in targets[:3]
        ],
        "evaluated": evaluated_mesh_stats(obj, deps),
    }


@command("set_shape_key", summary="set shape key influence, mute or slider range")
def _cmd_set_shape_key(
    object: str,
    key_name: str,
    value: float | None = None,
    mute: bool | None = None,
    slider_min: float | None = None,
    slider_max: float | None = None,
    clamp_to_slider: bool = False,
    set_active: bool = False,
):
    obj = get_mesh_object(object)
    block = _get_key_block(obj, key_name)
    requested = value
    if value is not None:
        new_value = float(value)
        if clamp_to_slider:
            new_value = max(block.slider_min, min(block.slider_max, new_value))
        block.value = new_value
    if mute is not None:
        block.mute = bool(mute)
    if slider_min is not None:
        block.slider_min = float(slider_min)
    if slider_max is not None:
        block.slider_max = float(slider_max)
    if set_active:
        obj.active_shape_key_index = list(obj.data.shape_keys.key_blocks).index(block)
    obj.data.update_tag()
    flush_object(obj)
    deps = depsgraph_update()
    driven = _driven_warning(obj.data.shape_keys, 'key_blocks["%s"].value' % block.name)
    warnings = []
    if driven:
        warnings.append(
            "shape key %r is driven (%s); a manually assigned value is overwritten on the next "
            "dependency-graph evaluation - change the driver input instead" % (block.name, driven["expressions"])
        )
    return {
        "object": obj.name,
        "key": block.name,
        "value": round(float(block.value), 6),
        "effective_value": round(float(block.value), 6),
        "requested_value": requested,
        "clamped": bool(clamp_to_slider and requested is not None and abs(float(requested) - float(block.value)) > 1e-9),
        "driven": bool(driven),
        "driver": driven,
        "warnings": warnings,
        "mute": bool(block.mute),
        "slider_min": block.slider_min,
        "slider_max": block.slider_max,
        "evaluated": evaluated_mesh_stats(obj, deps),
        "signature": mesh_signature(obj, deps),
    }


@command("remove_shape_key", summary="remove one shape key (Basis last)")
def _cmd_remove_shape_key(object: str, key_name: str):
    obj = get_mesh_object(object)
    block = _get_key_block(obj, key_name)
    blocks = obj.data.shape_keys.key_blocks
    if len(blocks) == 1:
        raise CommandError(
            "protected_key",
            "refusing to remove the only shape key (%r): it is the Basis" % block.name,
        )
    if blocks[0] is block:
        raise CommandError(
            "protected_key",
            "refusing to remove the Basis shape key %r; remove the object's shape keys instead" % block.name,
            details={"keys": [k.name for k in blocks]},
        )
    index = list(blocks).index(block)
    obj.shape_key_remove(block)
    flush_object(obj)
    depsgraph_update()
    report = _shape_key_report(obj)
    report["object"] = obj.name
    report["removed"] = key_name
    report["removed_index"] = index
    return report


# --------------------------------------------------------------------------- #
# commands: drivers
# --------------------------------------------------------------------------- #
def _resolve_driver_owner(owner_kind: str, owner_name: str | None):
    kind = (owner_kind or "").lower()
    if kind == "object":
        if not owner_name:
            raise CommandError("invalid_argument", "owner_name (object) is required for owner_kind='object'")
        owner = get_object(owner_name)
        return owner, "object:%s" % owner.name
    if kind == "shape_keys":
        if not owner_name:
            raise CommandError("invalid_argument", "owner_name (object) is required for owner_kind='shape_keys'")
        obj = get_mesh_object(owner_name)
        if obj.data.shape_keys is None:
            raise CommandError(
                "no_shape_keys",
                "object %r has no shape keys, so it has no shape key driver owner" % obj.name,
                details={"hint": "create a shape key first"},
            )
        return obj.data.shape_keys, "shape_keys:%s" % obj.name
    if kind == "node_group":
        if not owner_name:
            raise CommandError("invalid_argument", "owner_name (node group) is required for owner_kind='node_group'")
        group = get_node_group(owner_name)
        return group, "node_group:%s" % group.name
    if kind == "scene":
        scene = bpy.data.scenes.get(owner_name) if owner_name else bpy.context.scene
        if scene is None:
            raise CommandError("scene_not_found", "no scene named %r" % owner_name)
        return scene, "scene:%s" % scene.name
    raise CommandError(
        "invalid_argument",
        "owner_kind must be one of object, shape_keys, node_group, scene; got %r" % owner_kind,
    )


def _driver_payload(fcurve) -> dict:
    driver = fcurve.driver
    entry = {
        "data_path": fcurve.data_path,
        "array_index": fcurve.array_index,
        "mute": bool(fcurve.mute),
        "type": driver.type,
        "expression": driver.expression,
        "is_valid": bool(driver.is_valid),
        "use_self": bool(getattr(driver, "use_self", False)),
        "variables": [],
        "keyframe_count": len(fcurve.keyframe_points),
        "is_driver": True,
    }
    for var in driver.variables:
        targets = []
        for target in var.targets:
            entry_target = {}
            for attr in ("id_type", "data_path", "transform_type", "transform_space", "bone_target"):
                if hasattr(target, attr):
                    entry_target[attr] = _jsonable(getattr(target, attr))
            if hasattr(target, "id"):
                try:
                    entry_target["id_name"] = target.id.name if target.id else None
                    entry_target["id_rna"] = target.id.bl_rna.identifier if target.id else None
                except Exception:  # noqa: BLE001
                    entry_target["id_name"] = None
            targets.append(entry_target)
        entry["variables"].append({"name": var.name, "type": var.type, "targets": targets})
    return entry


def _driver_report(owner) -> list:
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    out = []
    for fcurve in animation.drivers:
        if getattr(fcurve, "driver", None) is not None:
            out.append(_driver_payload(fcurve))
    return out


@command("inspect_drivers", mutating=False, summary="drivers on object / shape_keys / node_group / scene")
def _cmd_inspect_drivers(owner_kind: str, owner_name: str | None = None):
    owner, label = _resolve_driver_owner(owner_kind, owner_name)
    return {
        "owner_kind": owner_kind,
        "owner": label,
        "owner_rna": owner.bl_rna.identifier,
        "drivers": _driver_report(owner),
        "animation": owner.animation_data is not None,
    }


def _validate_driver_expression(expression: str, variable_names: list) -> dict:
    """Static validation of a driver expression.

    The expression is *parsed* (``ast.parse`` in ``eval`` mode - parsing never
    executes anything) and the resulting tree is checked against a strict
    whitelist: literals, names, arithmetic/comparison/boolean operators, the
    ternary expression, and calls to a small set of maths functions.  Attributes,
    subscripts, lambdas, comprehensions, assignments and imports are rejected, so
    this channel cannot be turned into Python execution.
    """
    if expression is None or not str(expression).strip():
        raise CommandError("invalid_argument", "driver expression must not be empty")
    text = str(expression)
    for char in _DRIVER_EXPRESSION_FORBIDDEN_CHARS:
        if char in text:
            raise CommandError(
                "invalid_expression",
                "driver expression contains a rejected character %r; drivers accept a single "
                "arithmetic/boolean expression, not Python statements" % char,
                details={"expression": text},
            )
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise CommandError(
            "invalid_expression",
            "driver expression is not a valid single expression: %s" % exc.msg,
            details={"expression": text, "offset": exc.offset},
        ) from exc

    declared = {str(name) for name in variable_names if name}
    used_names: set[str] = set()
    rejected: list[str] = []
    undefined: list[str] = []
    allowed_nodes = (
        ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
        ast.BoolOp, ast.Compare, ast.IfExp, ast.Tuple, ast.List, ast.Call,
    )
    operator_nodes = (
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    )
    for node in ast.walk(tree):
        if isinstance(node, operator_nodes):
            continue
        if isinstance(node, ast.Name):
            used_names.add(node.id)
            if node.id.startswith("_") or node.id in _DRIVER_EXPRESSION_FORBIDDEN_NAMES:
                rejected.append(node.id)
            elif node.id not in declared and node.id not in _DRIVER_EXPRESSION_BUILTINS:
                undefined.append(node.id)
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in _DRIVER_EXPRESSION_BUILTINS:
                rejected.append("call to %s" % (getattr(func, "id", type(func).__name__)))
            if node.keywords:
                rejected.append("keyword arguments")
            continue
        if not isinstance(node, allowed_nodes):
            rejected.append(type(node).__name__)
    if rejected:
        raise CommandError(
            "invalid_expression",
            "driver expression uses unsupported construct(s): %s (drivers are not a Python exec channel)"
            % ", ".join(sorted(set(rejected))),
            details={"expression": text, "declared_variables": sorted(declared)},
        )
    if undefined:
        raise CommandError(
            "unknown_variable",
            "driver expression references undefined name(s): %s" % ", ".join(sorted(set(undefined))),
            details={"expression": text, "declared_variables": sorted(declared)},
        )
    return {"expression": text, "identifiers": sorted(used_names), "declared_variables": sorted(declared)}


def _resolve_target_id(id_type: str, id_name: str | None):
    collection_name = _DRIVER_ID_COLLECTIONS.get(str(id_type).upper())
    if collection_name is None:
        raise CommandError(
            "invalid_argument",
            "unsupported driver target id_type %r" % id_type,
            details={"supported": sorted(_DRIVER_ID_COLLECTIONS)},
        )
    if id_type.upper() == "SCENE" and not id_name:
        return bpy.context.scene
    collection = getattr(bpy.data, collection_name, None)
    if collection is None:
        raise CommandError("invalid_argument", "Blender has no %r data collection" % collection_name)
    if not id_name:
        raise CommandError("invalid_argument", "id_name is required for id_type=%s" % id_type)
    target = collection.get(str(id_name))
    if target is None:
        raise CommandError(
            "target_not_found",
            "no %s datablock named %r" % (id_type, id_name),
            details={"available": [d.name for d in collection][:50]},
        )
    return target


def _validate_path(owner, data_path: str) -> str:
    if not data_path or not isinstance(data_path, str):
        raise CommandError("invalid_argument", "data_path must be a non-empty string")
    try:
        owner.path_resolve(data_path, False)
    except (ValueError, TypeError, AttributeError) as exc:
        raise CommandError(
            "invalid_data_path",
            "data_path %r does not resolve on %s: %s" % (data_path, owner.bl_rna.identifier, exc),
            details={"owner": owner.bl_rna.identifier, "data_path": data_path},
        ) from exc
    return data_path


def _validate_variable_name(name, index: int) -> str:
    """Driver variable names must be usable identifiers in the expression."""
    if not isinstance(name, str) or not name.strip():
        raise CommandError("invalid_argument", "variable %d requires a non-empty 'name'" % index)
    name = name.strip()
    if not name.isidentifier():
        raise CommandError(
            "invalid_variable_name",
            "variable name %r is not a valid Python identifier, so it cannot appear in a driver expression"
            % name,
            details={"variable_index": index},
        )
    if keyword.iskeyword(name) or name in _DRIVER_EXPRESSION_FORBIDDEN_NAMES:
        raise CommandError(
            "invalid_variable_name",
            "variable name %r is a reserved/forbidden word and would shadow the driver sandbox" % name,
            details={"variable_index": index},
        )
    if name.startswith("_"):
        raise CommandError(
            "invalid_variable_name",
            "variable name %r must not start with an underscore" % name,
            details={"variable_index": index},
        )
    return name


def _prepare_variables(specs) -> list:
    """Validate every variable (name, uniqueness, targets) before any edit."""
    specs = list(specs or [])
    seen: dict[str, int] = {}
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise CommandError("invalid_argument", "each variable must be an object")
        name = _validate_variable_name(spec.get("name"), index)
        if name in seen:
            raise CommandError(
                "duplicate_variable",
                "variable name %r is declared twice (indexes %d and %d); driver expressions cannot have "
                "duplicate variables" % (name, seen[name], index),
                details={"name": name, "indexes": [seen[name], index]},
            )
        seen[name] = index
    return [_prepare_variable(spec) for spec in specs]


def _prepare_variable(spec) -> dict:
    """Validate one variable spec and resolve its targets WITHOUT mutating anything.

    Splitting validation from application means a rejected variable can never
    leave a half-configured driver behind: everything that can fail is checked
    before the driver is touched.
    """
    if not isinstance(spec, dict):
        raise CommandError("invalid_argument", "each variable must be an object")
    var_name = spec.get("name")
    var_type = str(spec.get("type", "SINGLE_PROP")).upper()
    if not var_name:
        raise CommandError("invalid_argument", "variable requires a 'name'")
    var_name = _validate_variable_name(var_name, 0)
    if var_type not in ("SINGLE_PROP", "TRANSFORMS"):
        raise CommandError(
            "invalid_argument",
            "variable type %r is not supported (supported: SINGLE_PROP, TRANSFORMS)" % var_type,
            details={"variable": spec},
        )
    targets = spec.get("targets") or []
    if not targets:
        raise CommandError("invalid_argument", "variable %r requires at least one target" % var_name)
    prepared_targets = []
    for index, target_spec in enumerate(targets):
        if not isinstance(target_spec, dict):
            raise CommandError("invalid_argument", "variable %r target %d must be an object" % (var_name, index))
        id_type = str(target_spec.get("id_type", "OBJECT")).upper()
        id_name = target_spec.get("id_name")
        if var_type == "TRANSFORMS" and id_type != "OBJECT":
            raise CommandError(
                "invalid_argument",
                "TRANSFORMS targets must use id_type='OBJECT' (got %r)" % id_type,
            )
        id_block = _resolve_target_id(id_type, id_name)
        prepared = {"id_type": id_type, "id_block": id_block, "id_name": getattr(id_block, "name", None)}
        if var_type == "SINGLE_PROP":
            data_path = target_spec.get("data_path")
            if not data_path:
                raise CommandError(
                    "invalid_argument",
                    "SINGLE_PROP target requires data_path (e.g. '[\"my_prop\"]' or 'location')",
                )
            _validate_path(id_block, data_path)
            prepared["data_path"] = data_path
        else:
            transform_type = target_spec.get("transform_type")
            if not transform_type:
                raise CommandError("invalid_argument", "TRANSFORMS target requires transform_type")
            transform_type = str(transform_type).upper()
            allowed = _enum_identifiers(bpy.types.DriverTarget, "transform_type")
            if allowed and transform_type not in allowed:
                raise CommandError(
                    "invalid_argument",
                    "transform_type %r is not supported" % transform_type,
                    details={"allowed": allowed},
                )
            space = str(target_spec.get("transform_space", "WORLD_SPACE")).upper()
            allowed_space = _enum_identifiers(bpy.types.DriverTarget, "transform_space")
            if allowed_space and space not in allowed_space:
                raise CommandError(
                    "invalid_argument",
                    "transform_space %r is not supported" % space,
                    details={"allowed": allowed_space},
                )
            prepared["transform_type"] = transform_type
            prepared["transform_space"] = space
            prepared["bone_target"] = target_spec.get("bone_target")
        prepared_targets.append(prepared)
    return {"name": str(var_name), "type": var_type, "targets": prepared_targets}


def _enum_identifiers(owner_type, property_name: str) -> list:
    """Enum identifiers of an RNA property, empty when it is not an enum."""
    try:
        prop = owner_type.bl_rna.properties[property_name]
    except (AttributeError, KeyError):
        return []
    return [item.identifier for item in prop.enum_items]


def _apply_variable(driver, prepared: dict) -> dict:
    """Assign an already validated variable spec onto a driver."""
    var = driver.variables.new()
    var.name = prepared["name"]
    var.type = prepared["type"]
    if len(prepared["targets"]) > len(var.targets):
        raise CommandError(
            "invalid_argument",
            "variable %r has %d targets but %s supports at most %d"
            % (prepared["name"], len(prepared["targets"]), prepared["type"], len(var.targets)),
        )
    built = []
    for index, spec in enumerate(prepared["targets"]):
        target = var.targets[index]
        try:
            current_type = str(target.id_type).upper()
        except AttributeError:
            current_type = spec["id_type"]
        if current_type != spec["id_type"]:
            try:
                target.id_type = spec["id_type"]
            except AttributeError as exc:
                raise CommandError(
                    "invalid_argument",
                    "%s targets always use id_type=%r; %r cannot be assigned"
                    % (prepared["type"], current_type, spec["id_type"]),
                ) from exc
        target.id = spec["id_block"]
        if prepared["type"] == "SINGLE_PROP":
            target.data_path = spec["data_path"]
        else:
            target.transform_type = spec["transform_type"]
            target.transform_space = spec["transform_space"]
            if spec.get("bone_target"):
                target.bone_target = str(spec["bone_target"])
        built.append(
            {
                "id_type": spec["id_type"],
                "id_name": spec["id_name"],
                "data_path": getattr(target, "data_path", None),
            }
        )
    return {"name": var.name, "type": var.type, "targets": built}


def _build_variable(driver, spec: dict) -> dict:
    """Validate then apply (kept for callers that pass raw specs)."""
    return _apply_variable(driver, _prepare_variable(spec))


def _find_driver_fcurves(owner, data_path: str, index: int = -1) -> list:
    """Locate driver FCurves by path; index < 0 matches every channel of the path."""
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return []
    return [
        fcurve
        for fcurve in animation.drivers
        if fcurve.data_path == data_path and (index < 0 or fcurve.array_index == index)
    ]


def _driven_warning(owner, data_path: str, index: int = -1):
    """Describe the driver(s) that will overwrite a manual write, if any."""
    fcurves = _find_driver_fcurves(owner, data_path, index)
    if not fcurves:
        return None
    return {
        "data_path": data_path,
        "channels": [f.array_index for f in fcurves],
        "expressions": [f.driver.expression for f in fcurves],
        "is_valid": [bool(f.driver.is_valid) for f in fcurves],
    }


def _clear_driver_variables(driver) -> int:
    removed = 0
    while len(driver.variables):
        driver.variables.remove(driver.variables[0])
        removed += 1
    return removed


def _snapshot_driver(driver) -> dict:
    """Capture everything needed to rebuild a driver after a failed update."""
    variables = []
    for var in driver.variables:
        targets = []
        for target in var.targets:
            entry = {"id_type": target.id_type}
            id_block = getattr(target, "id", None)
            entry["id_name"] = getattr(id_block, "name", None)
            for attr in ("data_path", "transform_type", "transform_space", "bone_target"):
                if hasattr(target, attr):
                    entry[attr] = getattr(target, attr)
            targets.append(entry)
        variables.append({"name": var.name, "type": var.type, "targets": targets})
    return {"expression": driver.expression, "type": driver.type, "variables": variables}


def _restore_driver(driver, snapshot: dict) -> list:
    """Rebuild a driver from a snapshot. Returns per-variable restore notes."""
    _clear_driver_variables(driver)
    driver.type = snapshot.get("type", "SCRIPTED")
    driver.expression = snapshot.get("expression", "")
    restored = []
    for spec in snapshot.get("variables", []):
        try:
            restored.append(_build_variable(driver, spec))
        except CommandError as exc:
            restored.append({"name": spec.get("name"), "restore_error": exc.message})
    return restored


def _driver_fcurves(owner, data_path: str, index: int):
    found = _find_driver_fcurves(owner, data_path, index)
    return found[0] if found else None


@command("add_driver", summary="add an expression driver to a supported owner")
def _cmd_add_driver(
    owner_kind: str,
    data_path: str,
    expression: str,
    owner_name: str | None = None,
    index: int = -1,
    variables: list | None = None,
    replace: bool = False,
):
    owner, label = _resolve_driver_owner(owner_kind, owner_name)
    _validate_path(owner, data_path)
    variables = variables or []
    # Variable names/duplicates and their targets are validated FIRST: a rejected
    # variable must never leave a driver (or its expression) half-edited.
    prepared_variables = _prepare_variables(variables)
    _validate_driver_expression(expression, [prepared["name"] for prepared in prepared_variables])

    if owner.animation_data is None:
        try:
            owner.animation_data_create()
        except (AttributeError, RuntimeError) as exc:
            raise CommandError(
                "unsupported_owner",
                "%s cannot own animation data: %s" % (label, exc),
            ) from exc
    existing_fcurves = _find_driver_fcurves(owner, data_path, index)
    created_channels = not existing_fcurves
    snapshots: dict[int, dict] = {}
    if existing_fcurves:
        if not replace:
            raise CommandError(
                "driver_exists",
                "a driver already exists at %s[%d] on %s (pass replace=true to overwrite it)"
                % (data_path, index, label),
                details={"existing": _driver_payload(existing_fcurves[0])},
            )
        # Snapshot every affected channel so a failed replacement can restore them.
        for fcurve in existing_fcurves:
            snapshots[fcurve.array_index] = _snapshot_driver(fcurve.driver)
            _clear_driver_variables(fcurve.driver)
        fcurves = _find_driver_fcurves(owner, data_path, index)
    else:
        try:
            added = owner.driver_add(data_path, index)
        except (TypeError, RuntimeError) as exc:
            raise CommandError(
                "driver_add_failed",
                "Blender refused to add a driver on %s at %r[%d]: %s" % (label, data_path, index, exc),
                details={"hint": "the property may not be animatable; try a custom property or another path"},
            ) from exc
        fcurves = list(added) if isinstance(added, list) else [added]

    def rollback(reason_summary: str) -> dict:
        """Restore replaced channels in full; delete only channels we created."""
        restored = {}
        if created_channels:
            try:
                owner.driver_remove(data_path, index)
            except (TypeError, RuntimeError):
                pass
        else:
            for fcurve in fcurves:
                snapshot = snapshots.get(fcurve.array_index)
                if snapshot is None:
                    continue
                restored[fcurve.array_index] = _restore_driver(fcurve.driver, snapshot)
        flush_owner(owner)
        depsgraph_update()
        return {"restored_channels": restored, "created_channels_removed": created_channels,
                "reason": reason_summary}

    built_variables = []
    try:
        for fcurve in fcurves:
            driver = fcurve.driver
            driver.type = "SCRIPTED"
            driver.expression = str(expression)
            for prepared in prepared_variables:
                built_variables.append(_apply_variable(driver, prepared))
    except CommandError as exc:
        exc.details = dict(exc.details or {})
        exc.details["rollback"] = rollback("variable application failed")
        raise
    flush_owner(owner)
    depsgraph_update()
    invalid = [f.array_index for f in fcurves if not f.driver.is_valid]
    if invalid:
        rollback_info = rollback("invalid expression")
        raise CommandError(
            "invalid_expression",
            "Blender reports the driver as invalid on channel(s) %s; %s"
            % (
                invalid,
                "the newly created driver was removed again"
                if created_channels
                else "the previous expression and variables were restored",
            ),
            details={"expression": str(expression), "data_path": data_path, "index": index,
                     "invalid_channels": invalid, "rollback": rollback_info,
                     "hint": "check that the expression only uses declared variables and maths functions"},
        )
    return {
        "owner": label,
        "data_path": data_path,
        "channels": [{"array_index": f.array_index, "is_valid": bool(f.driver.is_valid)} for f in fcurves],
        "expression": str(expression),
        "variables": built_variables,
        "drivers": _driver_report(owner),
        "evaluated_value": _safe_evaluated_value(owner, data_path, fcurves[0].array_index),
        "depsgraph": {"updated": True, "frame": bpy.context.scene.frame_current},
    }


def _safe_evaluated_value(owner, data_path: str, index: int):
    try:
        value = owner.path_resolve(data_path, True)
    except Exception:  # noqa: BLE001
        return None
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return _jsonable(value[index]) if index >= 0 else _jsonable(value)
        except Exception:  # noqa: BLE001
            return _jsonable(value)
    return _jsonable(value)


@command("update_driver", summary="update an existing driver's expression or variables")
def _cmd_update_driver(
    owner_kind: str,
    data_path: str,
    owner_name: str | None = None,
    index: int = -1,
    expression: str | None = None,
    variables: list | None = None,
    replace_variables: bool = False,
    mute: bool | None = None,
):
    owner, label = _resolve_driver_owner(owner_kind, owner_name)
    _validate_path(owner, data_path)
    fcurves = _find_driver_fcurves(owner, data_path, index)
    if not fcurves:
        raise CommandError(
            "driver_not_found",
            "no driver at %s[%d] on %s" % (data_path, index, label),
            details={"drivers": _driver_report(owner)},
        )
    # Validate new variable names/targets before touching the old ones, and snapshot
    # EVERY affected channel so a semantically invalid result can be rolled back whole.
    prepared_variables = _prepare_variables(variables) if variables is not None else None
    if expression is not None:
        declared = ([prepared["name"] for prepared in prepared_variables]
                    if prepared_variables is not None
                    else [v.name for v in fcurves[0].driver.variables])
        _validate_driver_expression(expression, declared)
    snapshots = {fcurve.array_index: _snapshot_driver(fcurve.driver) for fcurve in fcurves}

    built = []
    try:
        for fcurve in fcurves:
            driver = fcurve.driver
            if expression is not None:
                driver.expression = str(expression)
            if replace_variables:
                _clear_driver_variables(driver)
            if prepared_variables is not None:
                if not replace_variables and len(driver.variables) > 0:
                    _clear_driver_variables(driver)
                built.append([_apply_variable(driver, prepared) for prepared in prepared_variables])
            else:
                built.append([
                    {
                        "name": v.name,
                        "type": v.type,
                        "targets": [
                            {"id_type": t.id_type, "id_name": getattr(t.id, "name", None), "data_path": t.data_path}
                            for t in v.targets
                        ],
                    }
                    for v in driver.variables
                ])
            if mute is not None:
                fcurve.mute = bool(mute)
    except CommandError as exc:
        restored = {fcurve.array_index: _restore_driver(fcurve.driver, snapshots[fcurve.array_index])
                    for fcurve in fcurves}
        flush_owner(owner)
        depsgraph_update()
        exc.details = dict(exc.details or {})
        exc.details["rollback"] = {"restored_channels": sorted(restored), "reason": "variable application failed"}
        raise
    flush_owner(owner)
    depsgraph_update()
    warnings = []
    invalid = [fcurve.array_index for fcurve in fcurves if not fcurve.driver.is_valid]
    if invalid:
        # Blender cannot evaluate the new driver: put every previous channel back so
        # a failed update never destroys working animation.
        restored = {fcurve.array_index: _restore_driver(fcurve.driver, snapshots[fcurve.array_index])
                    for fcurve in fcurves}
        flush_owner(owner)
        depsgraph_update()
        raise CommandError(
            "invalid_expression",
            "Blender reports the new driver as invalid on channel(s) %s; the previous expression and "
            "variables were restored (is_valid=%s)"
            % (invalid, [bool(f.driver.is_valid) for f in fcurves]),
            details={
                "rejected_expression": str(expression) if expression is not None else None,
                "restored_expression": fcurves[0].driver.expression,
                "restored_variables": restored.get(fcurves[0].array_index),
                "is_valid_after_restore": [bool(f.driver.is_valid) for f in fcurves],
            },
        )
    return {
        "owner": label,
        "data_path": data_path,
        "channels": [{"array_index": f.array_index, "is_valid": bool(f.driver.is_valid)} for f in fcurves],
        "expression": fcurves[0].driver.expression,
        "is_valid": all(bool(f.driver.is_valid) for f in fcurves),
        "warnings": warnings,
        "variables": built[0] if built else [],
        "variables_per_channel": built,
        "drivers": _driver_report(owner),
        "evaluated_value": _safe_evaluated_value(owner, data_path, fcurves[0].array_index),
    }


@command("remove_driver", summary="remove a driver from a supported owner")
def _cmd_remove_driver(
    owner_kind: str,
    data_path: str,
    owner_name: str | None = None,
    index: int = -1,
):
    owner, label = _resolve_driver_owner(owner_kind, owner_name)
    fcurve = _driver_fcurves(owner, data_path, index)
    if fcurve is None:
        raise CommandError(
            "driver_not_found",
            "no driver at %s[%d] on %s" % (data_path, index, label),
            details={"drivers": _driver_report(owner)},
        )
    before = _driver_report(owner)
    try:
        owner.driver_remove(data_path, index)
    except (TypeError, RuntimeError) as exc:
        raise CommandError("driver_remove_failed", "Blender refused to remove the driver: %s" % exc) from exc
    flush_owner(owner)
    depsgraph_update()
    return {
        "owner": label,
        "data_path": data_path,
        "index": index,
        "removed": True,
        "drivers_before": len(before),
        "drivers": _driver_report(owner),
    }


# --------------------------------------------------------------------------- #
# server lifecycle + Blender integration
# --------------------------------------------------------------------------- #
_SERVER: BridgeServer | None = None
_TIMER_REGISTERED = False


def _log_path() -> Path:
    override = os.environ.get("BLENDER_MCP_LOG")
    return Path(override) if override else default_log_path()


def start_bridge(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    token_path: Path | None = None,
    use_timer: bool = True,
    log=None,
) -> BridgeServer:
    """Start (or return the running) bridge; optionally drive it from a timer."""
    global _SERVER, _TIMER_REGISTERED
    if _SERVER is not None and _SERVER.running:
        return _SERVER
    if token is None:
        token = read_or_create_token(token_path)
    logger = log or _Logger(_log_path())
    server = BridgeServer(host, int(port), token, logger)
    server.start()
    _SERVER = server
    if use_timer and not _TIMER_REGISTERED:
        bpy.app.timers.register(_timer_tick, first_interval=DEFAULT_TIMER_INTERVAL, persistent=True)
        _TIMER_REGISTERED = True
        logger("polling bridge from bpy.app.timers (main thread)")
    return server


def _timer_tick():
    global _TIMER_REGISTERED
    server = _SERVER
    if server is None or not server.running:
        _TIMER_REGISTERED = False
        return None
    try:
        server.poll()
    except Exception:  # noqa: BLE001 - never let the timer die silently
        try:
            server.log("timer poll crashed:\n%s" % traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass
    if server.stop_requested:
        _TIMER_REGISTERED = False
        return None
    return DEFAULT_TIMER_INTERVAL


def pump(seconds: float | None = None, interval: float = 0.01) -> dict:
    """Headless pump: call this in a loop when there is no Blender event loop."""
    server = _SERVER
    if server is None:
        raise CommandError("not_started", "bridge server is not started")
    deadline = None if not seconds else time.time() + float(seconds)
    iterations = 0
    while server.running and not server.stop_requested:
        server.poll()
        iterations += 1
        if deadline is not None and time.time() >= deadline:
            break
        time.sleep(interval)
    return {"iterations": iterations, "running": server.running, "stop_requested": server.stop_requested}


def stop_bridge(reason: str = "requested") -> bool:
    global _TIMER_REGISTERED
    if _SERVER is None:
        return False
    if _TIMER_REGISTERED:
        try:
            bpy.app.timers.unregister(_timer_tick)
        except Exception:  # noqa: BLE001
            pass
        _TIMER_REGISTERED = False
    _SERVER.stop(reason)
    return True


def probe_existing_bridge(host: str, port: int, token: str, timeout: float = 1.0) -> dict | None:
    """Return the health payload of an already-running bridge, else None."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            payload = json.dumps({"id": "probe", "token": token, "command": "health", "params": {}}) + "\n"
            sock.sendall(payload.encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer and len(buffer) < 1_000_000:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
            if b"\n" not in buffer:
                return None
            response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
            if response.get("ok"):
                return response.get("result")
    except (OSError, ValueError):
        return None
    return None


# --------------------------------------------------------------------------- #
# add-on registration (operators + panel)
# --------------------------------------------------------------------------- #
class BLENDERMCP_OT_start_bridge(bpy.types.Operator):
    bl_idname = "blendermcp.start_bridge"
    bl_label = "Start Blender MCP Bridge"
    bl_description = "Start (or reuse) the local token-authenticated JSON bridge on 127.0.0.1"

    def execute(self, context):
        try:
            server = start_bridge()
        except CommandError as exc:
            self.report({"ERROR"}, exc.message)
            return {"CANCELLED"}
        self.report({"INFO"}, "Blender MCP bridge listening on %s" % server.address)
        return {"FINISHED"}


class BLENDERMCP_OT_stop_bridge(bpy.types.Operator):
    bl_idname = "blendermcp.stop_bridge"
    bl_label = "Stop Blender MCP Bridge"
    bl_description = "Stop the local bridge (Blender itself keeps running)"

    def execute(self, context):
        stopped = stop_bridge("operator")
        self.report({"INFO"} if stopped else {"WARNING"}, "bridge stopped" if stopped else "bridge was not running")
        return {"FINISHED"}


class BLENDERMCP_OT_status(bpy.types.Operator):
    bl_idname = "blendermcp.status"
    bl_label = "Blender MCP Bridge Status"
    bl_description = "Print bridge status to the Blender console"

    def execute(self, context):
        if _SERVER is None:
            self.report({"INFO"}, "Blender MCP bridge is not running")
        else:
            status = _SERVER.status()
            self.report(
                {"INFO"},
                "bridge %s on %s | ok=%d failed=%d clients=%d"
                % ("running" if status["running"] else "stopped", status["address"],
                   status["requests_ok"], status["requests_failed"], status["clients"]),
            )
        return {"FINISHED"}


class BLENDERMCP_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP Bridge"
    bl_idname = "BLENDERMCP_PT_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "scene"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        status = _SERVER.status() if _SERVER else None
        box = layout.box()
        if status and status["running"]:
            box.label(text="Running on %s" % status["address"], icon="CHECKMARK")
            box.label(text="requests ok/failed: %d/%d" % (status["requests_ok"], status["requests_failed"]))
            box.label(text="clients: %d" % status["clients"])
            if status.get("last_error"):
                box.label(text="last error: %s" % status["last_error"], icon="ERROR")
        else:
            box.label(text="Not running", icon="X")
        box.label(text="token: MCP/.runtime/token")
        row = layout.row(align=True)
        row.operator("blendermcp.start_bridge", icon="PLAY")
        row.operator("blendermcp.stop_bridge", icon="PAUSE")
        layout.operator("blendermcp.status", icon="INFO")


_CLASSES = (
    BLENDERMCP_OT_start_bridge,
    BLENDERMCP_OT_stop_bridge,
    BLENDERMCP_OT_status,
    BLENDERMCP_PT_panel,
)


def register() -> None:
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except RuntimeError:
            pass  # already registered (bootstrap executed twice)
    _Logger(_log_path())("add-on registered (%d classes)" % len(_CLASSES))


def unregister() -> None:
    stop_bridge("add-on unregistered")
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
    server = start_bridge()
    print("Blender MCP bridge listening on %s" % server.address)
