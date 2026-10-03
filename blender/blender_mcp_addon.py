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
import base64
import hashlib
import ipaddress
import keyword
import math
import os
import re
import secrets
import socket
import stat
import struct
import sys
import time
import traceback
import uuid
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
# Actual NodeSocketFloat.type is VALUE in this Blender build. FLOAT is retained
# for callers/probes that report the RNA scalar category instead.
_NUMERIC_SOCKET_TYPES = {"FLOAT", "VALUE", "INT", "VECTOR", "COLOR", "ROTATION", "BOOLEAN"}
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
# audit log: every bridge command as one sanitised JSON line
# --------------------------------------------------------------------------- #
# The MCP server injects ``_mcp_audit`` into the request parameters (it contains
# only the correlation id and the server session), ``dispatch`` strips it before
# validating the parameters, and every command start/end is written to
# ``<MCP>/.runtime/logs/audit-bridge-<pid>-<session>.jsonl`` so the same live
# dashboard shows both halves of one operation.
AUDIT_VERSION = 1
AUDIT_PROTOCOL = 1
AUDIT_RESERVED_PARAM = "_mcp_audit"
AUDIT_MAX_FILE_BYTES = 2 * 1024 * 1024
AUDIT_KEEP_ARCHIVES = 5
AUDIT_MAX_STRING = 320
AUDIT_MAX_ITEMS = 20
AUDIT_MAX_DICT_KEYS = 64
AUDIT_MAX_DEPTH = 5
AUDIT_MAX_HASH_ITEMS = 200_000
AUDIT_FILE_PREFIX = "audit-"

_AUDIT_SECRET_MARKERS = (
    "token", "secret", "password", "passwd", "credential", "authorization",
    "api_key", "apikey", "private_key", "bearer",
)
_AUDIT_SECRET_SAFE_SUFFIXES = ("_present", "_path", "_file", "_count", "_available", "_supported", "_protocol")
_AUDIT_BINARY_SUFFIXES = ("base64", "b64")
_AUDIT_TOKEN_PATTERN = re.compile(r"(?i)\b(token|secret|password|passwd|bearer|api[_-]?key)\b\s*[:=]\s*[^\s,;\"')}]+")


def audit_now_iso() -> str:
    """UTC timestamp with millisecond resolution."""
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % int((now % 1) * 1000)


def _audit_is_secret_key(key) -> bool:
    lowered = str(key).lower()
    if lowered.endswith(_AUDIT_SECRET_SAFE_SUFFIXES):
        return False
    return any(marker in lowered for marker in _AUDIT_SECRET_MARKERS)


def _audit_is_binary_key(key) -> bool:
    return str(key).lower().endswith(_AUDIT_BINARY_SUFFIXES)


def _audit_sha256(value) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        raw = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


class _AuditSanitizer:
    """Bounded, secret-free rendering of command parameters and results."""

    def __init__(self, token: str | None = None):
        self._token = token or ""

    def scrub_text(self, text: str) -> str:
        out = str(text)
        if self._token and self._token in out:
            out = out.replace(self._token, "***")
        return _AUDIT_TOKEN_PATTERN.sub(lambda m: "%s=***" % m.group(1), out)

    def value(self, value, key=None, depth: int = 0):
        if key is not None:
            if _audit_is_secret_key(key):
                return "***"
            if _audit_is_binary_key(key):
                try:
                    length = len(value)
                except TypeError:
                    length = None
                return {"_type": "binary", "length": length, "sha256": _audit_sha256(value)}
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return str(value)
            return value
        if isinstance(value, str):
            text = self.scrub_text(value)
            if len(text) > AUDIT_MAX_STRING:
                return "%s…<+%d chars>" % (text[:AUDIT_MAX_STRING], len(text) - AUDIT_MAX_STRING)
            return text
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            return {"_type": "bytes", "length": len(raw), "sha256": _audit_sha256(raw)}
        if depth >= AUDIT_MAX_DEPTH:
            return {"_type": type(value).__name__, "truncated": "max_depth", "sha256": _audit_sha256(value)}
        if isinstance(value, dict):
            out = {}
            items = list(value.items())
            for name, item in items[:AUDIT_MAX_DICT_KEYS]:
                out[str(name)] = self.value(item, key=str(name), depth=depth + 1)
            if len(items) > AUDIT_MAX_DICT_KEYS:
                out["_truncated_keys"] = len(items) - AUDIT_MAX_DICT_KEYS
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            total = len(items)
            if total > AUDIT_MAX_ITEMS:
                summary = {
                    "_type": "array",
                    "length": total,
                    "sample_size": AUDIT_MAX_ITEMS,
                    "sample": [self.value(item, depth=depth + 1) for item in items[:AUDIT_MAX_ITEMS]],
                    "truncated": total - AUDIT_MAX_ITEMS,
                    "sha256": _audit_sha256(items) if total <= AUDIT_MAX_HASH_ITEMS else None,
                }
                return summary
            return [self.value(item, depth=depth + 1) for item in items]
        # bpy ID / RNA structs: keep the type and the name, never the whole object.
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return {"_type": type(value).__name__, "name": name}
        try:
            return self.scrub_text(repr(value))[:AUDIT_MAX_STRING]
        except Exception:  # noqa: BLE001
            return "<unrepresentable %s>" % type(value).__name__

    def mapping(self, values) -> dict:
        if not isinstance(values, dict):
            return {}
        return {str(k): self.value(v, key=str(k)) for k, v in list(values.items())[:AUDIT_MAX_DICT_KEYS]}


class _AuditLog:
    """One append-only JSONL file per Blender process, size-rotated with archives.

    Rotation is per process, so two Blender instances can never race on the same
    file; the dashboard keeps working across a rotation because it re-reads a file
    whose size shrank.
    """

    def __init__(self, directory: Path | None = None, source: str = "bridge",
                 max_bytes: int = AUDIT_MAX_FILE_BYTES, keep_archives: int = AUDIT_KEEP_ARCHIVES,
                 token: str | None = None, enabled: bool = True):
        self.directory = Path(directory) if directory else default_audit_dir()
        self.source = source
        self.max_bytes = max(4096, int(max_bytes))
        self.keep_archives = max(1, int(keep_archives))
        self.session = uuid.uuid4().hex[:12]
        self.pid = os.getpid()
        self.enabled = bool(enabled)
        self.sanitizer = _AuditSanitizer(token)
        self._seq = 0
        self._handle = None
        self._path = None
        self._archive_index = 0
        self.failures = 0
        self._last_notice = 0.0

    @property
    def name(self) -> str:
        return "%s%s-%d-%s.jsonl" % (AUDIT_FILE_PREFIX, self.source, self.pid, self.session)

    @property
    def path(self) -> Path:
        return self.directory / self.name

    def describe(self) -> dict:
        return {
            "directory": str(self.directory),
            "file": str(self.path),
            "session": self.session,
            "pid": self.pid,
            "enabled": self.enabled,
            "events": self._seq,
            "failures": self.failures,
            "limits": {
                "max_string": AUDIT_MAX_STRING,
                "max_items": AUDIT_MAX_ITEMS,
                "max_depth": AUDIT_MAX_DEPTH,
                "max_file_bytes": self.max_bytes,
            },
        }

    def notice(self, message: str) -> None:
        """Bounded stderr warning - stdout belongs to the host, never to us."""
        now = time.time()
        if now - self._last_notice < 30.0:
            return
        self._last_notice = now
        try:
            sys.stderr.write("[blender-mcp] audit %s\n" % self.sanitizer.scrub_text(message))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

    def _open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path = self.path
        self._handle = open(self._path, "a", encoding="utf-8", newline="\n")
        self._archive_index = self._next_archive_index()

    def _next_archive_index(self) -> int:
        stem = self._path.stem
        highest = 0
        try:
            for candidate in self.directory.glob("%s.*.jsonl" % stem):
                tail = candidate.name[len(stem) + 1: -len(".jsonl")]
                if tail.isdigit():
                    highest = max(highest, int(tail))
        except OSError:
            return 0
        return highest

    def _rotate(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        self._archive_index += 1
        archive = self.directory / ("%s.%03d.jsonl" % (self._path.stem, self._archive_index))
        try:
            if self._path.exists():
                os.replace(str(self._path), str(archive))
        except OSError:
            pass
        try:
            archives = sorted(self.directory.glob("%s.*.jsonl" % self._path.stem))
            for stale in archives[: max(0, len(archives) - self.keep_archives)]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        self._open()

    def record(self, event: str, **fields):
        """Append one sanitised event. Never raises: modelling must keep working."""
        if not self.enabled:
            return None
        try:
            self._seq += 1
            entry = {
                "v": AUDIT_VERSION,
                "ts": audit_now_iso(),
                "source": self.source,
                "session": self.session,
                "pid": self.pid,
                "seq": self._seq,
                "event": str(event),
            }
            entry.update(self.sanitizer.mapping(fields))
            line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.notice("could not build an audit entry: %s" % exc)
            return None
        try:
            if self._handle is None:
                self._open()
            if self._handle.tell() >= self.max_bytes:
                self._rotate()
            self._handle.write(line)
            self._handle.flush()
        except Exception as exc:  # noqa: BLE001
            self._handle = None
            self.failures += 1
            self.notice("log write failed (%d total): %s: %s" % (self.failures, type(exc).__name__, exc))
            try:
                self._open()
                self._handle.write(line)
                self._handle.flush()
            except Exception as exc2:  # noqa: BLE001
                self._handle = None
                self.failures += 1
                self.notice("log write failed again: %s" % exc2)
                return None
        return entry

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


_AUDIT: "_AuditLog | None" = None


def audit_log(create: bool = True) -> "_AuditLog | None":
    """The process-wide bridge audit log (created lazily on first use)."""
    global _AUDIT
    if _AUDIT is None and create:
        try:
            token = read_token()
        except CommandError:
            token = ""
        _AUDIT = _AuditLog(token=token)
    return _AUDIT


def configure_audit(directory=None, enabled: bool = True, **kwargs) -> "_AuditLog":
    """Replace the bridge audit log (used by tests to isolate directory/size)."""
    global _AUDIT
    existing = _AUDIT
    try:
        token = read_token()
    except CommandError:
        token = ""
    _AUDIT = _AuditLog(directory=directory, token=token, enabled=enabled, **kwargs)
    if existing is not None:
        existing.close()
    return _AUDIT


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


def default_audit_dir() -> Path:
    """Where the JSONL audit files live (overridable so tests stay isolated)."""
    override = os.environ.get("BLENDER_MCP_AUDIT_DIR")
    return Path(override) if override else (runtime_dir() / "logs")


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
    """Validate and run one bridge command, auditing it on the way in and out.

    The reserved ``_mcp_audit`` field (added by the MCP server so both halves of an
    operation share one request id) is removed *before* the parameter check, so it
    can never be mistaken for a command argument, and a caller that does not send
    it is completely unaffected.
    """
    request_id = None
    log = audit_log()
    clean = params
    if isinstance(params, dict) and AUDIT_RESERVED_PARAM in params:
        clean = dict(params)
        marker = clean.pop(AUDIT_RESERVED_PARAM)
        if isinstance(marker, dict):
            request_id = marker.get("request_id") or marker.get("session")
    entry = _COMMANDS.get(name)
    if entry is None:
        message = "unknown bridge command %r" % name
        if log is not None:
            log.record(
                "command_end", request_id=request_id, command=name, status="error", ok=False,
                code="unknown_command", message=message, validation=True, duration_ms=0.0,
            )
        raise CommandError(
            "unknown_command",
            message,
            details={"available": sorted(_COMMANDS)},
        )
    func = entry["func"]
    signature = inspect.signature(func)
    accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values())

    def _reject(code: str, message: str, details=None):
        if log is not None:
            log.record(
                "command_end", request_id=request_id, command=name, status="error", ok=False,
                code=code, message=message, validation=True, duration_ms=0.0,
            )
        raise CommandError(code, message, details=details)

    if not accepts_kwargs:
        accepted = [p.name for p in signature.parameters.values()]
        unknown = sorted(set(clean) - set(accepted))
        if unknown:
            _reject(
                "invalid_argument",
                "unknown parameter(s) for command %r: %s" % (name, ", ".join(unknown)),
                details={"accepted": accepted},
            )
        missing = [
            p.name
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and p.name not in clean
        ]
        if missing:
            _reject(
                "invalid_argument",
                "missing required parameter(s) for command %r: %s" % (name, ", ".join(missing)),
                details={"accepted": accepted},
            )
    if log is not None:
        log.record(
            "command_start", request_id=request_id, command=name, status="running",
            mutating=bool(entry["mutating"]), params=clean,
        )
    started = time.perf_counter()
    try:
        result = func(**clean)
    except CommandError as exc:
        if log is not None:
            log.record(
                "command_end", request_id=request_id, command=name, status="error", ok=False,
                code=exc.code, message=exc.message, details=exc.details,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
        raise
    except Exception as exc:  # noqa: BLE001 - auditing must not change behaviour
        if log is not None:
            log.record(
                "command_end", request_id=request_id, command=name, status="crash", ok=False,
                code="internal_error", message="%s: %s" % (type(exc).__name__, exc),
                duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            )
        raise
    if log is not None:
        log.record(
            "command_end", request_id=request_id, command=name, status="ok", ok=True,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            result=result,
        )
    return result


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
        "audit_protocol": AUDIT_PROTOCOL,
        "audit": audit_log(create=False).describe() if audit_log(create=False) is not None else None,
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
def _cmd_batch_graph_edit(group: str, operations: list, transactional: bool = True,
                          continue_on_error: bool = False):
    """Apply graph edits either as an isolated transaction (default) or in place.

    Transactional mode never touches the addressed node group until every
    operation has succeeded on a staging copy, so a failure cannot leave a
    half-edited graph behind.  The legacy in-place mode is unchanged and is the
    only mode in which ``continue_on_error`` is accepted.
    """
    if not isinstance(operations, list) or not operations:
        raise CommandError("invalid_argument", "operations must be a non-empty list")
    if transactional and continue_on_error:
        raise CommandError(
            "incompatible_options",
            "continue_on_error requires transactional=false: a transaction either commits as a whole or "
            "rolls back as a whole, so it cannot also continue past a failure (nothing has been changed)",
            details={"transactional": True, "continue_on_error": True},
        )
    tree = get_node_group(group)
    if transactional:
        return _transactional_batch_graph_edit(tree, operations)
    return _in_place_batch_graph_edit(tree, operations, continue_on_error)


def _batch_summary_entries(completed: list, failures: list) -> dict:
    return {
        "completed": completed,
        "failures": failures,
        "completed_count": len(completed),
        "failed_count": len(failures),
    }


def _run_batch_operations(tree, operations: list, continue_on_error: bool) -> tuple[list, list]:
    """Apply every operation to ``tree`` and report per-operation results."""
    completed, failures = [], []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            failures.append({"index": index, "op": None, "ok": False, "error": "operation must be an object"})
            if not continue_on_error:
                break
            continue
        op = operation.get("op")
        op_params = {k: v for k, v in operation.items() if k != "op"}
        try:
            handler = _BATCH_OPS.get(op)
            if handler is None:
                raise CommandError(
                    "unknown_operation",
                    "unknown batch op %r" % op,
                    details={"supported": sorted(_BATCH_OPS)},
                )
            result = handler(tree, op_params)
            completed.append({"index": index, "op": op, "ok": True, "result": _jsonable(result)})
        except CommandError as exc:
            entry = {"index": index, "op": op, "ok": False, "error": exc.message, "code": exc.code}
            if exc.details is not None:
                entry["details"] = _jsonable(exc.details)
            failures.append(entry)
            if not continue_on_error:
                break
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {"index": index, "op": op, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            )
            if not continue_on_error:
                break
    return completed, failures


def _in_place_batch_graph_edit(tree, operations: list, continue_on_error: bool) -> dict:
    """The legacy, explicitly non-atomic behaviour: partial edits stay applied."""
    completed, failures = _run_batch_operations(tree, operations, continue_on_error)
    flush_group_users(tree)
    depsgraph_update()
    payload = {
        "group": tree.name,
        **_batch_summary_entries(completed, failures),
        "transactional": False,
        "partial": bool(failures) and bool(completed),
        "rollback": False,
        "committed": False,
        "rolled_back": False,
        "transaction": {
            "mode": "in_place",
            "status": "partial" if failures else "completed",
            "note": "legacy non-atomic mode: edits listed in 'completed' are already part of the node group",
        },
        "note": (
            "operations are applied in order and are NOT transactional: edits listed in "
            "'completed' are already part of the node group and are not rolled back"
        ),
        "group_state": _inspect_group(tree, include_sockets=False),
    }
    return payload


# --------------------------------------------------------------------------- #
# transactional node-group editing
# --------------------------------------------------------------------------- #
def _same_datablock(a, b) -> bool:
    """Exact datablock identity (bpy wrappers are not ``is``-comparable)."""
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        return a.as_pointer() == b.as_pointer()
    except (AttributeError, TypeError, RuntimeError):
        try:
            return a.name == b.name
        except AttributeError:
            return False


def _iter_animation_owners():
    """Every ID in the file that could own animation data (version independent)."""
    seen = set()
    for name in dir(bpy.data):
        if name.startswith("_"):
            continue
        try:
            collection = getattr(bpy.data, name)
        except AttributeError:
            continue
        if not isinstance(collection, bpy.types.bpy_prop_collection):
            continue
        try:
            items = list(collection)
        except (TypeError, RuntimeError):
            continue
        for item in items:
            try:
                pointer = item.as_pointer()
            except Exception:  # noqa: BLE001
                pointer = id(item)
            if pointer in seen:
                continue
            seen.add(pointer)
            yield item


def _action_fcurves(action) -> list:
    """F-Curves of an action across the legacy and the slotted (4.4+/5.x) API."""
    fcurves = list(getattr(action, "fcurves", None) or [])
    for layer in getattr(action, "layers", None) or []:
        for strip in getattr(layer, "strips", None) or []:
            for channelbag in getattr(strip, "channelbags", None) or []:
                fcurves.extend(list(getattr(channelbag, "fcurves", None) or []))
    return fcurves


def _iter_driver_fcurves():
    """Every driver-carrying F-Curve in the file, without duplicates."""
    seen = set()
    for owner in _iter_animation_owners():
        animation = getattr(owner, "animation_data", None)
        if animation is None:
            continue
        for fcurve in list(getattr(animation, "drivers", None) or []):
            try:
                key = fcurve.as_pointer()
            except Exception:  # noqa: BLE001
                key = id(fcurve)
            if key in seen:
                continue
            seen.add(key)
            yield fcurve
    for action in list(bpy.data.actions):
        for fcurve in _action_fcurves(action):
            if getattr(fcurve, "driver", None) is None:
                continue
            try:
                key = fcurve.as_pointer()
            except Exception:  # noqa: BLE001
                key = id(fcurve)
            if key in seen:
                continue
            seen.add(key)
            yield fcurve


def _collect_group_references(group) -> list:
    """Every RNA pointer in the file that currently points at ``group``.

    Each entry is ``(kind, label, owner, attribute, current_value)``.  Modifier
    inputs, nested group nodes and driver targets are the three ways a geometry
    node tree can be referenced in Blender; anything left over is caught by the
    ``users`` check right before the original datablock is removed.
    """
    refs = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if getattr(mod, "type", None) == "NODES" and _same_datablock(getattr(mod, "node_group", None), group):
                refs.append(("modifier", "%s/%s" % (obj.name, mod.name), mod, "node_group", mod.node_group))
    for tree in bpy.data.node_groups:
        if _same_datablock(tree, group):
            continue
        for node in tree.nodes:
            if _same_datablock(getattr(node, "node_tree", None), group):
                refs.append(
                    ("node_group_node", "%s/%s" % (tree.name, node.name), node, "node_tree", node.node_tree)
                )
    for fcurve in _iter_driver_fcurves():
        driver = getattr(fcurve, "driver", None)
        if driver is None:
            continue
        for variable in driver.variables:
            for target in variable.targets:
                if _same_datablock(getattr(target, "id", None), group):
                    refs.append((
                        "driver_target",
                        "%s.%s -> %s" % (getattr(fcurve, "data_path", "?"), variable.name,
                                         getattr(target, "data_path", "")),
                        target, "id", target.id,
                    ))
    return refs


def _copy_idprop_value(value):
    if isinstance(value, list):
        return [_copy_idprop_value(item) for item in value]
    return value


def _snapshot_modifier_inputs(mod) -> dict:
    """Per-instance geometry-nodes values keyed by socket identifier."""
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is None:
        return {}
    try:
        keys = list(inputs.keys())
    except Exception:  # noqa: BLE001
        return {}
    snapshot = {}
    for key in keys:
        item = inputs[key]
        try:
            if "value" not in item.keys():
                continue
            snapshot[key] = _copy_idprop_value(item["value"])
        except Exception:  # noqa: BLE001
            continue
    return snapshot


def _restore_modifier_inputs(mod, snapshot: dict) -> tuple[int, list]:
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is None:
        return 0, sorted(snapshot)
    restored, missing = 0, []
    for key, value in snapshot.items():
        try:
            if key not in inputs.keys():
                missing.append(key)
                continue
            inputs[key]["value"] = _copy_idprop_value(value)
            restored += 1
        except Exception as exc:  # noqa: BLE001
            missing.append("%s (%s)" % (key, exc))
    return restored, missing


def _sync_tree_animation(original, staging) -> dict:
    """Carry the original node tree's animation data over to the staging copy.

    ``ID.copy()`` normally copies animation data too, but that is a Blender
    implementation detail, so it is verified here and repaired when missing - a
    driver on a node-group socket must survive the swap.
    """
    source = getattr(original, "animation_data", None)
    info = {
        "source_has_animation": source is not None,
        "source_action": getattr(getattr(source, "action", None), "name", None),
        "source_drivers": len(list(getattr(source, "drivers", None) or [])) if source else 0,
        "target_had_animation": getattr(staging, "animation_data", None) is not None,
        "repaired_action": False,
    }
    if source is None:
        return info
    target = getattr(staging, "animation_data", None)
    if target is None:
        try:
            target = staging.animation_data_create()
        except Exception as exc:  # noqa: BLE001
            info["error"] = "%s: %s" % (type(exc).__name__, exc)
            return info
    action = getattr(source, "action", None)
    if action is not None and getattr(target, "action", None) is None:
        try:
            target.action = action
            info["repaired_action"] = True
        except Exception as exc:  # noqa: BLE001
            info["error"] = "%s: %s" % (type(exc).__name__, exc)
    info["target_action"] = getattr(getattr(staging, "animation_data", None), "action", None) is not None
    info["target_drivers"] = len(list(getattr(getattr(staging, "animation_data", None), "drivers", None) or []))
    return info


def _revert_group_references(remapped: list) -> int:
    restored = 0
    for _kind, _label, owner, attribute, original in reversed(remapped):
        try:
            setattr(owner, attribute, None)
        except Exception:  # noqa: BLE001
            pass
        try:
            setattr(owner, attribute, original)
            restored += 1
        except Exception:  # noqa: BLE001
            pass
    return restored


def _flush_remapped(remapped: list) -> int:
    """Tag every ID that was re-pointed so its dependency graph is rebuilt.

    Re-pointing a pointer does not by itself invalidate a modifier, a nested node
    or a driver variable: the owning ID has to be tagged, otherwise the cached
    evaluation (and the driver's cached value) keeps using the old datablock.
    """
    tagged = 0
    for _kind, _label, owner, _attribute, _original in remapped:
        candidates = []
        try:
            data = getattr(owner, "id_data", None)
            if data is not None:
                candidates.append(data)
        except Exception:  # noqa: BLE001
            pass
        for candidate in candidates:
            try:
                candidate.update_tag()
                tagged += 1
            except Exception:  # noqa: BLE001
                pass
    return tagged


def _drop_datablock(tree) -> bool:
    try:
        bpy.data.node_groups.remove(tree)
        return True
    except Exception:  # noqa: BLE001
        return False


def _transactional_batch_graph_edit(tree, operations: list) -> dict:
    """Stage every edit on an isolated copy and swap it in only on full success."""
    library = getattr(tree, "library", None)
    override = getattr(tree, "override_library", None)
    if library is not None or override is not None:
        raise CommandError(
            "unsafe_linked_group",
            "node group %r comes from a linked library (or library override) and cannot be edited "
            "transactionally; make it local first" % tree.name,
            details={"library": getattr(library, "filepath", None), "override": bool(override)},
        )

    staging = tree.copy()
    staging.name = "%s.__mcp_stage__" % tree.name
    completed, failures = _run_batch_operations(staging, operations, False)

    if failures:
        staging_name = staging.name
        removed = _drop_datablock(staging)
        depsgraph_update()
        return {
            "group": tree.name,
            **_batch_summary_entries(completed, failures),
            "transactional": True,
            "partial": False,
            "rollback": True,
            "committed": False,
            "rolled_back": True,
            "transaction": {
                "mode": "isolated_staging",
                "status": "rolled_back",
                "staging": {"created": True, "name": staging_name, "removed": removed},
                "original": {"name": tree.name, "unchanged": True, "removed": False},
                "datablock_identity_replaced": False,
                "failed_index": failures[0].get("index"),
                "note": (
                    "no operation was applied to the original group: the whole batch ran on an isolated "
                    "staging copy which has been removed, so nodes, links, defaults, interface, modifier "
                    "inputs, nested group references and drivers are exactly as they were"
                ),
            },
            "note": "transaction rolled back; the addressed node group was not modified",
            "group_state": _inspect_group(tree, include_sockets=False),
        }

    # -- commit ------------------------------------------------------------- #
    refs = _collect_group_references(tree)
    counts = {"modifier": 0, "node_group_node": 0, "driver_target": 0}
    for kind, _label, _owner, _attr, _value in refs:
        counts[kind] = counts.get(kind, 0) + 1
    snapshots = [
        (label, owner, _snapshot_modifier_inputs(owner))
        for kind, label, owner, _attr, _value in refs
        if kind == "modifier"
    ]
    animation = _sync_tree_animation(tree, staging)

    original_name = tree.name
    original_fake_user = bool(getattr(tree, "use_fake_user", False))
    staging_fake_user = bool(getattr(staging, "use_fake_user", False))
    remapped = []
    remap_mode = "manual"
    renamed = False

    def _undo_commit(reason: str) -> dict:
        """Put every user back on the original and drop the staging copy.

        Used by every failure path after the rename, so a failed commit can never
        leave the original with a cleared fake user, a lost modifier value or a
        leaked backup datablock.
        """
        info = {"reason": reason, "remap_mode": remap_mode}
        if remap_mode == "native":
            info["reversed_users"] = _reverse_native_remap(staging, tree)
        else:
            info["reverted_references"] = _revert_group_references(remapped)
        info["modifier_inputs"] = {"restored": 0, "missing": []}
        for label, mod, snapshot in snapshots:
            restored, missing = _restore_modifier_inputs(mod, snapshot)
            info["modifier_inputs"]["restored"] += restored
            info["modifier_inputs"]["missing"].extend("%s:%s" % (label, key) for key in missing)
        try:
            tree.use_fake_user = original_fake_user
            info["fake_user_restored"] = bool(getattr(tree, "use_fake_user", False))
        except Exception as exc:  # noqa: BLE001
            info["fake_user_restore_error"] = "%s: %s" % (type(exc).__name__, exc)
        _revert_rename(tree, staging, original_name, renamed)
        info["staging_removed"] = _drop_datablock(staging)
        info["leaked_groups"] = _leaked_preview_groups()
        depsgraph_update()
        return info

    try:
        staging.name = "%s.__mcp_stage__" % original_name
        tree.name = "%s.__mcp_old__" % original_name
        staging.name = original_name
        renamed = True
        native_remap = getattr(tree, "user_remap", None)
        if callable(native_remap):
            # Blender's own remap walks every ID that references the datablock -
            # including references this build does not expose to Python - and keeps
            # modifier input values, nested links/defaults and socket identifiers.
            remap_mode = "native"
            native_remap(staging)
        else:  # pragma: no cover - every supported build has ID.user_remap
            for kind, label, owner, attribute, original_value in refs:
                if kind == "driver_target":
                    # Clearing first forces Blender to rebuild the driver variable's
                    # dependency relation; a plain re-assign can leave the cached
                    # relation (and the driver's evaluated value) on the old datablock.
                    try:
                        setattr(owner, attribute, None)
                    except Exception:  # noqa: BLE001
                        pass
                setattr(owner, attribute, staging)
                remapped.append((kind, label, owner, attribute, original_value))
    except Exception as exc:  # noqa: BLE001
        info = _undo_commit("swap_failed")
        raise CommandError(
            "transaction_failed",
            "the node group swap failed and was undone: %s: %s" % (type(exc).__name__, exc),
            details={"references": len(refs), "rollback": info},
        ) from exc

    # Nothing may still reference the original, otherwise removing it would leave
    # a dangling pointer - undo instead.
    try:
        tree.use_fake_user = False
    except Exception:  # noqa: BLE001
        pass
    remaining_users = int(getattr(tree, "users", 0))
    if remaining_users > 0:
        info = _undo_commit("unremappable_references")
        raise CommandError(
            "unremappable_references",
            "the node group is still referenced by %d ID pointer(s) this build does not expose; the "
            "transaction was undone and the original group is untouched" % remaining_users,
            details={"remaining_users": remaining_users, "remapped": counts, "rollback": info},
        )

    input_report = {"snapshots": len(snapshots), "restored": 0, "missing": []}
    for label, mod, snapshot in snapshots:
        restored, missing = _restore_modifier_inputs(mod, snapshot)
        input_report["restored"] += restored
        for key in missing:
            input_report["missing"].append("%s:%s" % (label, key))
    if input_report["missing"]:
        # A lost per-instance value is a real data loss: roll the swap back rather
        # than reporting a commit that silently dropped the user's parameters.
        info = _undo_commit("modifier_inputs_lost")
        raise CommandError(
            "modifier_inputs_lost",
            "the new node group does not expose %d modifier input(s) the object had before; the "
            "transaction was undone so no parameter value is lost" % len(input_report["missing"]),
            details={"modifier_inputs": input_report, "rollback": info},
        )

    original_removed = _drop_datablock(tree)
    if not original_removed:
        info = _undo_commit("original_removal_failed")
        raise CommandError(
            "transaction_failed",
            "the previous node group could not be removed after the swap; the transaction was undone "
            "instead of leaving a leaked backup datablock",
            details={"rollback": info},
        )
    try:
        staging.use_fake_user = staging_fake_user or original_fake_user
    except Exception:  # noqa: BLE001
        pass
    # Tag every enumerated owner in both modes: user_remap re-points the pointers,
    # but the owning IDs still need a tag so modifiers/drivers re-evaluate.
    tagged = _flush_remapped(refs)
    flush_group_users(staging)
    depsgraph_update()

    payload = {
        "group": staging.name,
        **_batch_summary_entries(completed, failures),
        "transactional": True,
        "partial": False,
        "rollback": False,
        "committed": True,
        "rolled_back": False,
        "transaction": {
            "mode": "isolated_staging",
            "status": "committed",
            "staging": {"created": True, "name": staging.name, "kept": True},
            "original": {"name": original_name, "removed": original_removed},
            "datablock_identity_replaced": bool(original_removed),
            "remap_mode": remap_mode,
            "users_remapped": len(remapped) if remap_mode == "manual" else counts_total(counts),
            "remapped": counts,
            "tagged_ids": tagged,
            "modifier_inputs": input_report,
            "animation": animation,
            "fake_user_preserved": bool(getattr(staging, "use_fake_user", False)) == (
                original_fake_user or staging_fake_user),
            "note": (
                "success replaces the node group datablock identity: the edited staging tree is promoted "
                "to the original name and every user is re-pointed at it (%s remap), so modifier input "
                "values, driver targets, nested references and names/interface identifiers are preserved. "
                "This is a node-group-level transaction, not a blend-file undo: references held outside "
                "bpy (other add-ons caching the old datablock) are not rewritten."
                % ("native ID.user_remap" if remap_mode == "native" else "enumerated")
            ),
        },
        "note": (
            "transaction committed: the node group now contains the staged edits; the previous datablock "
            "was unlinked and removed (no leaked staging or backup groups)"
        ),
        "group_state": _inspect_group(staging, include_sockets=False),
    }
    return payload


def counts_total(counts: dict) -> int:
    return sum(int(value) for value in counts.values())


def _leaked_preview_groups() -> list:
    """Any staging/backup node group left behind (must always be empty)."""
    return sorted(group.name for group in bpy.data.node_groups if "__mcp_" in group.name)


def _reverse_native_remap(staging, original) -> int:
    """Undo ``user_remap`` after a failed commit: move every user back."""
    remap = getattr(staging, "user_remap", None)
    if not callable(remap):
        return -1
    try:
        remap(original)
        return int(getattr(original, "users", 0))
    except Exception:  # noqa: BLE001
        return -1


def _revert_rename(tree, staging, original_name: str, renamed: bool) -> None:
    """Put the original name back after a failed swap (best effort)."""
    if not renamed:
        return
    try:
        staging.name = "%s.__mcp_stage_failed__" % original_name
    except Exception:  # noqa: BLE001
        pass
    try:
        tree.name = original_name
    except Exception:  # noqa: BLE001
        pass



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
# commands: model preview capture
# --------------------------------------------------------------------------- #
PREVIEW_SUBDIR = "previews"
PREVIEW_MIN_DIMENSION = 16
PREVIEW_MAX_DIMENSION = 2048
PREVIEW_MAX_BYTES = 3 * 1024 * 1024
PREVIEW_MAX_SAMPLES = 256
PREVIEW_TIMEOUT_DEFAULT = 45.0
PREVIEW_TEMP_PREFIX = "__MCP_PREVIEW_"
#: Border kept around the fitted model (12% total), so nothing touches the edge.
PREVIEW_FIT_MARGIN = 1.12
#: Tried in order until one engine produces a picture (headless Windows may lack
#: a GPU context for EEVEE, and Workbench is the fastest deterministic option).
PREVIEW_ENGINE_CHAIN = ("BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES")
PREVIEW_VIEW_DIRECTIONS = {
    "isometric": (1.0, -1.0, 0.7),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0),
}


def _preview_modes():
    return ("auto", "viewport", "render")


def _resolve_preview_target(relative_path):
    """Validate ``previews/<name>.png`` and return ``(absolute_path, relative)``."""
    if not relative_path or not str(relative_path).strip():
        raise CommandError("invalid_argument", "relative_path must not be empty")
    raw = str(relative_path).strip().replace("\\", "/")
    if not raw.lower().endswith(".png"):
        raise CommandError("invalid_extension", "preview files must end with .png, got %r" % raw)
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise CommandError("invalid_path", "preview path must be relative to <workspace>/Project")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CommandError("invalid_path", "path traversal ('..') is rejected")
    if len(parts) < 2 or parts[0].lower() != PREVIEW_SUBDIR:
        raise CommandError(
            "invalid_path",
            "preview files must be written to <workspace>/Project/%s/*.png, got %r" % (PREVIEW_SUBDIR, raw),
        )
    relative = "/".join(parts)
    resolved = resolve_project_path(relative)
    root = Path(os.path.realpath(str(project_dir() / PREVIEW_SUBDIR)))
    if resolved != root and root not in resolved.parents:
        raise CommandError("invalid_path", "resolved preview path escapes %s" % root)
    # The directory is created only when a capture is actually published, so a
    # request that fails validation (or a viewport request in --background) never
    # touches the file system.
    return resolved, relative


def _ensure_preview_directory(target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CommandError(
            "preview_dir_unwritable",
            "cannot create the preview directory %s: %s" % (target.parent, exc),
            details={"directory": str(target.parent), "errno": getattr(exc, "errno", None)},
        ) from exc


def _preview_objects(name=None):
    """The mesh objects a preview should show, scoped to the CURRENT view layer.

    Scanning ``bpy.data.objects`` pulled meshes from unrelated scenes into the
    preview; the active view layer is what the user is actually looking at (and
    what a viewport capture would show).
    """
    if name:
        obj = get_object(name)
        if obj.type != "MESH":
            raise CommandError(
                "wrong_object_type",
                "object %r is %s; previews need a MESH object" % (obj.name, obj.type),
                details={"object": obj.name, "type": obj.type},
            )
        return [obj]
    selected = []
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        if obj.type != "MESH" or obj.name.startswith(PREVIEW_TEMP_PREFIX):
            continue
        if bool(getattr(obj, "hide_render", False)):
            continue
        try:
            if not obj.visible_get():
                continue
        except Exception:  # noqa: BLE001 - not in this view layer
            continue
        selected.append(obj)
    return selected


#: Cap on the instance vertices gathered per preview, so a million-instance graph
#: cannot stall the capture.
MAX_INSTANCE_POINTS = 200_000


def _instance_points(deps, obj):
    """World-space points of the instances the dependency graph produced for ``obj``.

    A Geometry Nodes graph that only *instances* geometry (Instance on Points and
    friends) leaves the evaluated mesh empty; the instances themselves are exposed
    through ``depsgraph.object_instances`` and are what must be framed.
    """
    points = []
    seen = 0
    truncated = False
    for instance in deps.object_instances:
        parent = getattr(instance, "parent", None)
        if parent is None or getattr(parent, "name", None) != obj.name:
            continue
        if not getattr(instance, "is_instance", True):
            continue  # the original object itself is listed too
        source = getattr(instance, "instance_object", None) or getattr(instance, "object", None)
        if source is None or getattr(source, "type", None) != "MESH":
            continue
        seen += 1
        try:
            evaluated = source.evaluated_get(deps)
            mesh = evaluated.to_mesh()
        except Exception:  # noqa: BLE001
            continue
        try:
            matrix = instance.matrix_world
            for vertex in mesh.vertices:
                points.append(tuple(matrix @ vertex.co))
                if len(points) >= MAX_INSTANCE_POINTS:
                    truncated = True
                    break
        finally:
            evaluated.to_mesh_clear()
        if truncated:
            break
    return points, seen, truncated


def _evaluated_bounds(objects):
    """World-space bounds of the EVALUATED geometry (modifiers + instances)."""
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    mins = [None, None, None]
    maxs = [None, None, None]
    per_object = {}
    total_points = 0
    instances_total = 0
    instance_points_total = 0
    truncated_any = False

    def add_point(point):
        nonlocal total_points
        total_points += 1
        for axis in range(3):
            value = float(point[axis])
            if mins[axis] is None or value < mins[axis]:
                mins[axis] = value
            if maxs[axis] is None or value > maxs[axis]:
                maxs[axis] = value

    for obj in objects:
        entry = {"vertices": 0, "instances": 0, "instance_vertices": 0,
                 "truncated": False, "error": None}
        try:
            evaluated = obj.evaluated_get(deps)
            mesh = evaluated.to_mesh()
        except Exception as exc:  # noqa: BLE001
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            per_object[obj.name] = entry
            continue
        try:
            matrix = evaluated.matrix_world
            for vertex in mesh.vertices:
                add_point(matrix @ vertex.co)
                entry["vertices"] += 1
        finally:
            evaluated.to_mesh_clear()
        try:
            points, seen, truncated = _instance_points(deps, obj)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = "instances: %s: %s" % (type(exc).__name__, exc)
            points, seen, truncated = [], 0, False
        entry["instances"] = seen
        entry["truncated"] = truncated
        instances_total += seen
        truncated_any = truncated_any or truncated
        for point in points:
            add_point(point)
            entry["instance_vertices"] += 1
        instance_points_total += len(points)
        per_object[obj.name] = entry

    if total_points == 0 or mins[0] is None:
        return None, per_object
    center = [(mins[i] + maxs[i]) / 2.0 for i in range(3)]
    size = [maxs[i] - mins[i] for i in range(3)]
    source = "evaluated mesh vertices in world space (dependency graph)"
    if instance_points_total:
        source += " + %d instance vertices" % instance_points_total
    return {
        "min": [round(v, 6) for v in mins],
        "max": [round(v, 6) for v in maxs],
        "center": [round(v, 6) for v in center],
        "size": [round(v, 6) for v in size],
        "radius": round(max(max(size) / 2.0, 1e-4), 6),
        "vertices": total_points,
        "mesh_vertices": total_points - instance_points_total,
        "instance_vertices": instance_points_total,
        "instances": instances_total,
        "instances_truncated": truncated_any,
        "objects": per_object,
        "source": source,
    }, per_object


def _empty_geometry_details(objects, per_object, instances_total: int) -> dict:
    """An actionable explanation when the evaluated model has no measurable points."""
    details = {"objects": per_object, "instances": instances_total}
    if instances_total:
        details["hint"] = (
            "the dependency graph reports %d instance(s) but exposed no vertices for them. "
            "Add a 'Realize Instances' node to the graph (or pass object=<realized mesh>) so the "
            "preview can frame and render them" % instances_total
        )
        details["code_hint"] = "realize_instances"
    else:
        details["hint"] = (
            "check the Geometry Nodes graph / modifiers: a node group that outputs no geometry "
            "produces an empty preview"
        )
    return details


def _preview_temp_dir() -> Path:
    path = runtime_dir() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _look_at_euler(origin, target_point):
    """Camera rotation euler looking from ``origin`` at ``target_point``.

    ``mathutils`` is bundled with Blender but is imported here (not at module
    scope) so the module stays importable under the unit-test ``bpy`` stub.
    """
    from mathutils import Vector  # noqa: PLC0415 - bundled with Blender

    vector = Vector(target_point) - Vector(origin)
    return vector.to_track_quat("-Z", "Y").to_euler()


def _preset_view_quaternion(direction):
    from mathutils import Vector  # noqa: PLC0415 - bundled with Blender

    return Vector(direction).to_track_quat("Z", "Y")


def _fit_ortho_camera(camera, corners, width, height) -> dict:
    """Frame the whole world-space box in an orthographic camera.

    The previous heuristic (``max_world_size * 2.6``) clipped rotated and elongated
    models. This projects the eight corners into camera space and fits BOTH image
    axes with a margin: with ``sensor_fit = 'HORIZONTAL'`` the orthographic scale is
    the horizontal extent, so the required scale is
    ``max(projected_width, projected_height * width / height) * margin``.
    """
    from mathutils import Matrix, Vector  # noqa: PLC0415 - bundled with Blender

    # Build the camera transform from its location/rotation instead of reading
    # matrix_world: the camera lives in the temporary scene, so the *current* view
    # layer update does not refresh its cached matrix (a stale identity matrix made
    # the fit - and the clip planes - wrong, which clipped the model away).
    world = Matrix.Translation(camera.location) @ camera.rotation_euler.to_matrix().to_4x4()
    inverse = world.inverted()
    projected = [inverse @ Vector(corner) for corner in corners]
    xs = [point.x for point in projected]
    ys = [point.y for point in projected]
    depths = [-point.z for point in projected]  # positive in front of the camera
    projected_width = max(xs) - min(xs)
    projected_height = max(ys) - min(ys)
    aspect = float(width) / float(max(int(height), 1))
    camera.data.type = "ORTHO"
    try:
        camera.data.sensor_fit = "HORIZONTAL"
    except Exception:  # noqa: BLE001 - very old builds
        pass
    scale = max(projected_width, projected_height * aspect) * PREVIEW_FIT_MARGIN
    camera.data.ortho_scale = max(scale, 1e-3)
    near = max(min(depths) * 0.5, 1e-4)
    camera.data.clip_start = near
    camera.data.clip_end = max(max(depths) * 2.0, near + 1e-3)
    visible_width = camera.data.ortho_scale
    visible_height = camera.data.ortho_scale / max(aspect, 1e-6)
    return {
        "projected_width": round(projected_width, 4),
        "projected_height": round(projected_height, 4),
        "ortho_scale": round(camera.data.ortho_scale, 4),
        "sensor_fit": "HORIZONTAL",
        "aspect": round(aspect, 4),
        "margin": PREVIEW_FIT_MARGIN,
        "visible_width": round(visible_width, 4),
        "visible_height": round(visible_height, 4),
        "fits_width": bool(projected_width <= visible_width + 1e-6),
        "fits_height": bool(projected_height <= visible_height + 1e-6),
        "clip_start": round(near, 4),
        "clip_end": round(camera.data.clip_end, 4),
    }


def _bounds_corners(bounds) -> list:
    low = bounds["min"]
    high = bounds["max"]
    return [
        (x, y, z)
        for x in (low[0], high[0])
        for y in (low[1], high[1])
        for z in (low[2], high[2])
    ]


def _png_header(path: Path) -> dict:
    raw = path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CommandError("invalid_image", "Blender did not write a PNG to %s" % path)
    width, height = struct.unpack(">II", raw[16:24])
    return {"bytes": len(raw), "width": int(width), "height": int(height), "magic": raw[:8].hex()}


def _apply_engine_settings(scene, engine: str, samples, remaining: float) -> dict:
    used = {"engine": engine, "samples": None, "time_limit_s": None}
    try:
        if engine == "CYCLES":
            if samples:
                scene.cycles.samples = max(1, min(int(samples), PREVIEW_MAX_SAMPLES))
            used["samples"] = int(getattr(scene.cycles, "samples", 0) or 0)
            if remaining and remaining > 1.0:
                scene.cycles.time_limit = float(remaining)
                used["time_limit_s"] = round(float(remaining), 2)
        elif engine.startswith("BLENDER_EEVEE"):
            if samples:
                try:
                    scene.eevee.taa_render_samples = max(1, min(int(samples), PREVIEW_MAX_SAMPLES))
                except AttributeError:
                    pass
            used["samples"] = int(getattr(scene.eevee, "taa_render_samples", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        used["settings_error"] = "%s: %s" % (type(exc).__name__, exc)
    return used


def _save_render_result(target: Path, scene) -> None:
    image = bpy.data.images.get("Render Result")
    if image is None:
        raise CommandError("render_result_missing", "Blender produced no render result image")
    try:
        image.save_render(filepath=str(target), scene=scene)
    except TypeError:  # pragma: no cover - older/newer signature
        image.save_render(filepath=str(target))


def _capture_render(objects, bounds, target, width, height, view, samples, timeout_s, transparent):
    """Camera render inside a throwaway scene; the user's scene is never edited."""
    started = time.perf_counter()
    source_scene = bpy.context.scene
    diagnostics = {"mode": "render", "attempts": [], "scene": None}
    scene = bpy.data.scenes.new(PREVIEW_TEMP_PREFIX + "SCENE")
    created_objects = []
    created_data = []
    try:
        scene.render.resolution_x = int(width)
        scene.render.resolution_y = int(height)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA" if transparent else "RGB"
        scene.render.film_transparent = bool(transparent)
        # The temp scene must evaluate exactly like the source scene: same frame,
        # subframe and time base (a graph driven by frame/scene properties would
        # otherwise render a different model than the metadata describes).
        try:
            scene.frame_start = source_scene.frame_start
            scene.frame_end = source_scene.frame_end
            scene.frame_current = source_scene.frame_current
            scene.frame_subframe = source_scene.frame_subframe
            scene.render.fps = source_scene.render.fps
            scene.render.fps_base = source_scene.render.fps_base
        except Exception as exc:  # noqa: BLE001
            diagnostics["time_error"] = "%s: %s" % (type(exc).__name__, exc)
        copied_props = {}
        for key in source_scene.keys():
            try:
                scene[key] = source_scene[key]
                copied_props[key] = source_scene[key]
            except Exception:  # noqa: BLE001
                continue
        diagnostics["source_scene"] = source_scene.name
        diagnostics["time"] = {
            "frame": int(source_scene.frame_current),
            "subframe": round(float(source_scene.frame_subframe), 6),
            "fps": float(source_scene.render.fps),
            "fps_base": float(source_scene.render.fps_base),
        }
        diagnostics["scene_properties_copied"] = sorted(copied_props)
        for obj in objects:
            try:
                scene.collection.objects.link(obj)
            except RuntimeError:
                pass  # already linked

        camera_data = bpy.data.cameras.new(PREVIEW_TEMP_PREFIX + "CAM")
        created_data.append(camera_data)
        camera = bpy.data.objects.new(PREVIEW_TEMP_PREFIX + "CAM", camera_data)
        created_objects.append(camera)
        scene.collection.objects.link(camera)
        scene.camera = camera

        center = bounds["center"]
        radius = max(bounds["radius"], 1e-3)
        use_preset = True
        if view == "current":
            source = bpy.context.scene.camera
            if source is not None:
                try:
                    camera.matrix_world = source.matrix_world.copy()
                    camera_data.type = source.data.type
                    camera_data.lens = source.data.lens
                    camera_data.ortho_scale = source.data.ortho_scale
                    camera_data.clip_start = source.data.clip_start
                    camera_data.clip_end = source.data.clip_end
                    use_preset = False
                    diagnostics["note"] = "view='current' reused the scene camera transform"
                except Exception as exc:  # noqa: BLE001
                    diagnostics["note"] = "scene camera could not be reused (%s); used isometric" % exc
            else:
                diagnostics["note"] = "view='current' found no scene camera; used isometric"
        if use_preset:
            direction = PREVIEW_VIEW_DIRECTIONS.get(view, PREVIEW_VIEW_DIRECTIONS["isometric"])
            distance = radius * 4.0 + 1.0
            camera.location = (
                center[0] + direction[0] * distance,
                center[1] + direction[1] * distance,
                center[2] + direction[2] * distance,
            )
            camera.rotation_euler = _look_at_euler(camera.location, center)
            fit = _fit_ortho_camera(camera, _bounds_corners(bounds), width, height)
            diagnostics["camera"] = {"view": view, "distance": round(distance, 4), **fit}
            if not (fit["fits_width"] and fit["fits_height"]):  # pragma: no cover - defensive
                diagnostics.setdefault("notes", []).append(
                    "the projected bounds did not fit after the orthographic fit")
        else:
            diagnostics["camera"] = {
                "view": "current", "distance": None,
                "source_camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None,
                "ortho_scale": round(camera_data.ortho_scale, 4), "type": camera_data.type,
            }

        light_data = bpy.data.lights.new(PREVIEW_TEMP_PREFIX + "LIGHT", type="SUN")
        created_data.append(light_data)
        light_data.energy = 3.0
        light = bpy.data.objects.new(PREVIEW_TEMP_PREFIX + "LIGHT", light_data)
        created_objects.append(light)
        scene.collection.objects.link(light)
        light.rotation_euler = (0.7, 0.2, 0.5)

        world = bpy.data.worlds.new(PREVIEW_TEMP_PREFIX + "WORLD")
        created_data.append(world)
        world.color = (0.06, 0.06, 0.07) if not transparent else (0.0, 0.0, 0.0)
        scene.world = world
        diagnostics["scene"] = scene.name

        errors = []
        for engine in PREVIEW_ENGINE_CHAIN:
            remaining = float(timeout_s) - (time.perf_counter() - started)
            if remaining <= 0:
                errors.append({"engine": engine, "error": "preview timeout budget exhausted"})
                break
            attempt = {"engine": engine}
            engine_started = time.perf_counter()
            try:
                # The engine enum is dynamic, so availability is decided by trying.
                scene.render.engine = engine
                attempt.update(_apply_engine_settings(scene, engine, samples, remaining))
                bpy.ops.render.render(scene=scene.name)
                _save_render_result(target, scene)
                attempt["seconds"] = round(time.perf_counter() - engine_started, 3)
                attempt["ok"] = True
                diagnostics["attempts"].append(attempt)
                diagnostics["engine"] = engine
                diagnostics["seconds"] = attempt["seconds"]
                return diagnostics
            except Exception as exc:  # noqa: BLE001
                attempt["ok"] = False
                attempt["seconds"] = round(time.perf_counter() - engine_started, 3)
                attempt["error"] = "%s: %s" % (type(exc).__name__, exc)
                diagnostics["attempts"].append(attempt)
                errors.append({"engine": engine, "error": attempt["error"]})
        raise CommandError(
            "render_failed",
            "no render engine produced an image in this Blender build",
            details={"attempts": errors, "seconds": round(time.perf_counter() - started, 3)},
        )
    finally:
        for obj in created_objects:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:  # noqa: BLE001
                pass
        for data in created_data:
            try:
                if isinstance(data, bpy.types.Camera):
                    bpy.data.cameras.remove(data)
                elif isinstance(data, bpy.types.Light):
                    bpy.data.lights.remove(data)
                elif isinstance(data, bpy.types.World):
                    bpy.data.worlds.remove(data)
            except Exception:  # noqa: BLE001
                pass
        try:
            bpy.data.scenes.remove(scene)
        except Exception:  # noqa: BLE001
            pass
        diagnostics["cleaned_up"] = True


def _find_view3d_context():
    """A real, renderable 3D viewport, or ``None`` when there is none.

    ``--background`` still reports a window manager window on Blender 5.x, but it
    has no OpenGL context, so background is rejected up front and the OpenGL call
    is additionally guarded below.
    """
    if bool(getattr(bpy.app, "background", False)):
        return None
    try:
        windows = list(bpy.context.window_manager.windows)
    except Exception:  # noqa: BLE001
        return None
    for window in windows:
        try:
            screen = window.screen
            areas = list(screen.areas) if screen else []
        except Exception:  # noqa: BLE001
            continue
        for area in areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region is not None and region.width > 0 and region.height > 0:
                return window, screen, area, region
    return None


def _request_own_window_foreground():
    """Request activation of an own-process window, never synthesize input."""
    if os.name != "nt":
        return {"requested": True, "activated": False, "reason": "foreground activation is supported on Windows only"}
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    candidates = []
    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
            candidates.append(hwnd)
        return True
    user32.EnumWindows(visit, 0)
    if not candidates:
        return {"requested": True, "activated": False, "reason": "no visible window owned by this Blender process"}
    hwnd = candidates[0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    activated = user32.GetForegroundWindow() == hwnd
    return {"requested": True, "activated": activated, "pid": os.getpid(),
            "reason": None if activated else "Windows declined foreground activation; select the Blender taskbar window"}


@command("present_object", summary="select and frame an object in the GUI, optionally requesting own-window foreground")
def _cmd_present_object(object: str, view: str = "isometric", isolate: bool = True, foreground: bool = False):
    if view not in ("current",) + tuple(PREVIEW_VIEW_DIRECTIONS):
        raise CommandError("invalid_view", "unknown viewport direction", details={"view": view})
    if not isinstance(isolate, bool) or not isinstance(foreground, bool):
        raise CommandError("invalid_argument", "isolate and foreground must be booleans")
    obj = get_object(object)
    context = _find_view3d_context()
    if context is None:
        raise CommandError("viewport_unavailable", "present_object requires a GUI Blender 3D viewport")
    window, screen, area, region = context
    layer = window.view_layer
    if obj.name not in layer.objects:
        raise CommandError("object_not_in_view_layer", "object is outside the displayed view layer")
    space = area.spaces.active
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
        if bpy.context.mode != "OBJECT":
            raise CommandError("object_mode_required", "leave edit/pose mode before presenting an object")
        if space.local_view is not None:
            bpy.ops.view3d.localview(frame_selected=False)
        obj.hide_set(False, view_layer=layer)
        obj.hide_viewport = False
        for other in layer.objects:
            if other.select_get(view_layer=layer):
                other.select_set(False, view_layer=layer)
        obj.select_set(True, view_layer=layer)
        layer.objects.active = obj
        if isolate:
            bpy.ops.view3d.localview(frame_selected=False)
        if view != "current":
            space.region_3d.view_rotation = _preset_view_quaternion(PREVIEW_VIEW_DIRECTIONS[view])
            space.region_3d.view_perspective = "ORTHO"
        bpy.ops.view3d.view_selected(use_all_regions=False)
        for panel in screen.areas:
            if panel.type == "PROPERTIES":
                panel.spaces.active.context = "MODIFIER"
        area.tag_redraw()
    activation = _request_own_window_foreground() if foreground else {"requested": False, "activated": False}
    return {"object": obj.name, "selected": obj.select_get(view_layer=layer),
            "active": layer.objects.active.name, "local_view": space.local_view is not None,
            "view": view, "view_distance": float(space.region_3d.view_distance),
            "view_location": list(space.region_3d.view_location), "foreground": activation,
            "persistent": True, "note": "selection and view intentionally persist; geometry is unchanged"}


def _capture_viewport(objects, target, width, height, view, transparent, timeout_s):
    """Capture the real 3D viewport; requires a GUI Blender."""
    context = _find_view3d_context()
    if context is None:
        raise CommandError(
            "viewport_unavailable",
            "no 3D viewport exists in this Blender session (it runs in --background), so mode='viewport' "
            "cannot be honoured; use mode='render' or mode='auto', or run a GUI Blender",
            details={"background": bool(bpy.app.background), "windows": 0},
        )
    window, screen, area, region = context
    scene = bpy.context.scene
    diagnostics = {"mode": "viewport", "attempts": [], "restored": True,
                   "region": [int(region.width), int(region.height)]}
    snapshot = _snapshot_scene_state(scene)
    space = None
    try:
        space = next((item for item in area.spaces if item.type == "VIEW_3D"), None)
    except Exception:  # noqa: BLE001
        space = None
    region_state = None
    try:
        if space is not None and view != "current":
            region_3d = space.region_3d
            region_state = {
                "view_rotation": region_3d.view_rotation.copy(),
                "view_location": region_3d.view_location.copy(),
                "view_perspective": region_3d.view_perspective,
                "view_distance": region_3d.view_distance,
            }
        scene.render.resolution_x = int(width)
        scene.render.resolution_y = int(height)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = bool(transparent)
        with bpy.context.temp_override(window=window, screen=screen, area=area, region=region, scene=scene):
            if space is not None and view != "current":
                direction = PREVIEW_VIEW_DIRECTIONS.get(view, PREVIEW_VIEW_DIRECTIONS["isometric"])
                region_3d = space.region_3d
                region_3d.view_perspective = "ORTHO"
                region_3d.view_rotation = _preset_view_quaternion(direction)
                try:
                    bpy.ops.object.select_all(action="DESELECT")
                    for obj in objects:
                        obj.select_set(True)
                    bpy.context.view_layer.objects.active = objects[0]
                    bpy.ops.view3d.view_selected(use_all_regions=False)
                    diagnostics["framed"] = True
                except Exception as exc:  # noqa: BLE001
                    diagnostics["framed"] = False
                    diagnostics["frame_error"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                bpy.ops.render.opengl(write_still=False, view_context=True)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(
                    "viewport_unavailable",
                    "Blender refused the viewport capture (%s); use mode='render' or mode='auto' instead"
                    % exc,
                    details={"mode": "viewport", "error": str(exc)},
                ) from exc
        _save_render_result(target, scene)
        diagnostics["engine"] = "OPENGL_VIEWPORT"
        return diagnostics
    finally:
        if region_state is not None and space is not None:
            try:
                region_3d = space.region_3d
                region_3d.view_rotation = region_state["view_rotation"]
                region_3d.view_location = region_state["view_location"]
                region_3d.view_perspective = region_state["view_perspective"]
                region_3d.view_distance = region_state["view_distance"]
                diagnostics["viewport_view_restored"] = True
            except Exception as exc:  # noqa: BLE001
                diagnostics["viewport_view_restored"] = False
                diagnostics["viewport_restore_error"] = "%s: %s" % (type(exc).__name__, exc)
        _restore_scene_state(scene, snapshot, diagnostics)


def _snapshot_scene_state(scene) -> dict:
    render = scene.render
    view_layer = bpy.context.view_layer
    try:
        selected = sorted(obj.name for obj in bpy.context.selected_objects)
    except Exception:  # noqa: BLE001
        selected = []
    try:
        active = view_layer.objects.active
        active_name = active.name if active is not None else None
    except Exception:  # noqa: BLE001
        active_name = None
    return {
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "resolution_percentage": render.resolution_percentage,
        "filepath": render.filepath,
        "file_format": render.image_settings.file_format,
        "film_transparent": render.film_transparent,
        "engine": render.engine,
        "camera": scene.camera.name if scene.camera else None,
        "frame": scene.frame_current,
        "frame_subframe": scene.frame_subframe,
        "fps": render.fps,
        "fps_base": render.fps_base,
        "selected": selected,
        "active": active_name,
        "mode": bpy.context.mode,
    }


def _restore_scene_state(scene, snapshot: dict, diagnostics: dict) -> None:
    render = scene.render
    changes = []
    try:
        for key in ("resolution_x", "resolution_y", "resolution_percentage"):
            if getattr(render, key) != snapshot[key]:
                setattr(render, key, snapshot[key])
                changes.append(key)
        if render.filepath != snapshot["filepath"]:
            render.filepath = snapshot["filepath"]
            changes.append("filepath")
        if render.image_settings.file_format != snapshot["file_format"]:
            render.image_settings.file_format = snapshot["file_format"]
            changes.append("file_format")
        if bool(render.film_transparent) != bool(snapshot["film_transparent"]):
            render.film_transparent = snapshot["film_transparent"]
            changes.append("film_transparent")
        if render.engine != snapshot["engine"]:
            render.engine = snapshot["engine"]
            changes.append("engine")
        camera = bpy.data.objects.get(snapshot["camera"]) if snapshot["camera"] else None
        if scene.camera is not camera:
            scene.camera = camera
            changes.append("camera")
        if scene.frame_current != snapshot["frame"]:
            scene.frame_current = snapshot["frame"]
            changes.append("frame")
        if scene.frame_subframe != snapshot["frame_subframe"]:
            scene.frame_subframe = snapshot["frame_subframe"]
            changes.append("frame_subframe")
        if render.fps != snapshot["fps"]:
            render.fps = snapshot["fps"]
            changes.append("fps")
        if render.fps_base != snapshot["fps_base"]:
            render.fps_base = snapshot["fps_base"]
            changes.append("fps_base")
        # Selection and the active object are part of the visible editor state a
        # viewport capture changes, so they are restored as well.
        wanted = set(snapshot.get("selected") or [])
        try:
            for name in wanted:
                obj = bpy.data.objects.get(name)
                if obj is None:
                    continue
                if not obj.select_get():
                    obj.select_set(True)
                    changes.append("select:%s" % name)
            for obj in list(bpy.context.selected_objects):
                if obj.name not in wanted:
                    obj.select_set(False)
                    changes.append("deselect:%s" % obj.name)
            active = bpy.data.objects.get(snapshot["active"]) if snapshot.get("active") else None
            view_layer = bpy.context.view_layer
            if view_layer.objects.active is not active:
                view_layer.objects.active = active
                changes.append("active")
        except Exception as exc:  # noqa: BLE001
            diagnostics["selection_restore_error"] = "%s: %s" % (type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        diagnostics["restored"] = False
        diagnostics["restore_error"] = "%s: %s" % (type(exc).__name__, exc)
        return
    diagnostics["restored"] = True
    diagnostics["restored_fields"] = changes


@command("capture_preview", summary="write a model preview PNG (viewport capture or camera render)")
def _cmd_capture_preview(object: str | None = None, relative_path: str | None = None, mode: str = "auto",
                         view: str = "isometric", width: int = 1280, height: int = 720,
                         samples=None, timeout_s: float = PREVIEW_TIMEOUT_DEFAULT,
                         overwrite: bool = False, transparent: bool = False):
    """Render or capture the model and return the PNG as base64 plus diagnostics."""
    started = time.perf_counter()
    mode = str(mode or "auto").strip().lower()
    if mode not in _preview_modes():
        raise CommandError("invalid_argument", "mode must be one of %s" % (list(_preview_modes()),),
                           details={"mode": mode})
    view = str(view or "isometric").strip().lower()
    if view not in ("current",) + tuple(PREVIEW_VIEW_DIRECTIONS):
        raise CommandError(
            "invalid_argument",
            "view must be one of ['current', %s]" % ", ".join(sorted(PREVIEW_VIEW_DIRECTIONS)),
            details={"view": view},
        )
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError) as exc:
        raise CommandError("invalid_argument", "width/height must be integers") from exc
    for label, value in (("width", width), ("height", height)):
        if value < PREVIEW_MIN_DIMENSION or value > PREVIEW_MAX_DIMENSION:
            raise CommandError(
                "invalid_argument",
                "%s must be between %d and %d pixels (got %d)" % (label, PREVIEW_MIN_DIMENSION,
                                                                  PREVIEW_MAX_DIMENSION, value),
            )
    try:
        timeout_s = float(timeout_s or PREVIEW_TIMEOUT_DEFAULT)
    except (TypeError, ValueError) as exc:
        raise CommandError("invalid_argument", "timeout_s must be a number") from exc
    if timeout_s <= 0 or timeout_s > 300:
        raise CommandError("invalid_argument", "timeout_s must be between 0 and 300 seconds")

    target, relative = _resolve_preview_target(relative_path)
    if target.exists() and not overwrite:
        raise CommandError(
            "file_exists",
            "%s already exists; pass overwrite=true to replace it" % target,
            details={"path": str(target)},
        )

    objects = _preview_objects(object)
    if not objects:
        raise CommandError(
            "empty_geometry",
            "no visible MESH object is available to preview in the current view layer",
            details={"hint": "create geometry first, or pass object=<name>",
                     "scenes": sorted(item.name for item in bpy.data.scenes),
                     "current_scene": bpy.context.scene.name},
        )
    bounds, per_object = _evaluated_bounds(objects)
    if bounds is None:
        instances_total = sum(int(entry.get("instances") or 0) for entry in per_object.values())
        details = _empty_geometry_details(objects, per_object, instances_total)
        raise CommandError(
            "empty_geometry",
            "the selected object(s) evaluate to zero vertices, so there is nothing to show",
            details=details,
        )

    mode_used = mode
    notes = []
    if mode == "auto":
        mode_used = "viewport" if _find_view3d_context() is not None else "render"
        notes.append("mode='auto' selected '%s'" % mode_used)
    if mode_used == "viewport" and _find_view3d_context() is None:
        raise CommandError(
            "viewport_unavailable",
            "mode='viewport' needs a GUI Blender with a 3D viewport; this session has none "
            "(background=%s). Use mode='render' or mode='auto'." % bool(bpy.app.background),
            details={"background": bool(bpy.app.background), "requested_mode": mode},
        )

    temp_target = _preview_temp_dir() / ("preview-%s.png" % uuid.uuid4().hex[:12])
    try:
        if mode_used == "viewport":
            diagnostics = _capture_viewport(objects, temp_target, width, height, view, transparent, timeout_s)
        else:
            # 'current' reaches the render path unchanged: it reuses the scene
            # camera transform (and only falls back to isometric with a note).
            diagnostics = _capture_render(objects, bounds, temp_target, width, height,
                                          view, samples, timeout_s, transparent)
        header = _png_header(temp_target)
        if header["bytes"] > PREVIEW_MAX_BYTES:
            raise CommandError(
                "preview_too_large",
                "the rendered PNG is %.2f MiB, above the %.2f MiB cap; lower width/height or samples"
                % (header["bytes"] / 1048576.0, PREVIEW_MAX_BYTES / 1048576.0),
                details=header,
            )
        # Atomic publication: the rendered file is on the same filesystem/volume as
        # the target, so os.replace swaps it in one step. An existing preview is
        # NEVER unlinked first - if the replace fails the previous image survives.
        _ensure_preview_directory(target)
        try:
            os.replace(str(temp_target), str(target))
        except OSError as exc:
            raise CommandError(
                "preview_write_failed",
                "cannot publish the preview to %s: %s (the previous file, if any, is untouched)"
                % (target, exc),
                details={"path": str(target), "errno": getattr(exc, "errno", None),
                         "previous_kept": True},
            ) from exc
        header = _png_header(target)
    finally:
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass

    raw = target.read_bytes()
    payload = {
        "path": str(target),
        "relative_path": relative,
        "mode": mode_used,
        "requested_mode": mode,
        "view": view,
        "width": header["width"],
        "height": header["height"],
        "size": {"width": header["width"], "height": header["height"]},
        "bytes": header["bytes"],
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "transparent": bool(transparent),
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "evaluated": {
            "vertices": bounds["vertices"],
            "mesh_vertices": bounds.get("mesh_vertices"),
            "instance_vertices": bounds.get("instance_vertices"),
            "instances": bounds.get("instances"),
            "instances_truncated": bounds.get("instances_truncated"),
            "bounds": {"min": bounds["min"], "max": bounds["max"], "size": bounds["size"]},
            "radius": bounds["radius"],
            "objects": sorted(bounds["objects"]),
            "source": bounds["source"],
        },
        "limits": {
            "max_dimension": PREVIEW_MAX_DIMENSION,
            "max_bytes": PREVIEW_MAX_BYTES,
            "timeout_s": timeout_s,
        },
        "render": {
            "engine": diagnostics.get("engine"),
            "attempts": diagnostics.get("attempts"),
            "seconds": diagnostics.get("seconds"),
        },
        "diagnostics": {**diagnostics, "notes": notes},
        "scene_state": {
            "saved_blend": False,
            "note": (
                "the .blend is never saved by this command; render settings, the scene camera, the frame "
                "and every temporary datablock are restored even when the capture fails"
            ),
        },
    }
    return payload


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
