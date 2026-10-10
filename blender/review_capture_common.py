"""Shared bridge-side plumbing for **real** GUI screenshots (add-on side).

This module is imported by the optional command modules
``model_inspection.py`` and ``geometry_nodes_capture.py``.  Like them it is
registered by ``blender_mcp_addon.py`` and only ever imports ``bpy`` plus the
standard library.

What it provides
----------------
* **Path safety / publication** - a capture may only be published to
  ``<workspace>/Project/previews/<name>.png``.  The PNG is first written to
  ``<MCP>/.runtime/tmp/`` and then *atomically* moved into place with
  ``os.replace``, so a failed capture never truncates an existing preview.
  Every published file is re-read afterwards and verified (PNG signature,
  IHDR dimensions, byte size, SHA-256, mtime).
* **UI state machine** - the full editor state of the target ``window``/``area``
  (area type, every scalar space property, the node editor's tree/pin/path/UI
  region, the 3D view's ``region_3d``, the window scene, the selection and the
  active object) can be snapshotted, temporarily changed and restored - also
  when the capture fails.  What could *not* be restored is reported, never
  hidden.
* **Draw probe** - a real ``Space.draw_handler_add`` callback records how many
  frames were drawn after the state change *and reads the space state inside the
  draw pass*.  A capture is only committed when a draw was observed showing the
  requested scene/node tree, so a stale front buffer or a fallen-back editor
  cannot be returned as if it were the requested picture.
* **Deferred capture session** - ``prepare``/``status``/``commit``/``abort``
  bridge commands share one session record.  The bridge handler itself is
  synchronous (the add-on polls the socket from a ``bpy.app.timers`` callback),
  so the redraw happens in the main loop *between* two bridge calls: the MCP
  tool waits for ``status.draws_after_arm > 0`` and only then commits.

Deliberate limits (never faked)
-------------------------------
* ``bpy.ops.screen.screenshot_area`` captures the whole editor area; the image
  is **never** cropped or synthesised here.  A subset request changes only the
  view/fit and the machine-readable metadata.
* No mouse/keyboard synthesis, no window activation, no focus stealing.
* Nothing is saved to ``.blend``; scene data (meshes, modifiers, transforms,
  visibility) is never modified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import struct
import time
import uuid
from pathlib import Path

import bpy

#: Only place a capture may be published to (relative to ``<workspace>/Project``).
PREVIEW_SUBDIR = "previews"
#: Sanity bounds for a captured PNG; the byte cap below is the binding limit.
MIN_IMAGE_DIMENSION = 8
MAX_IMAGE_DIMENSION = 8192
#: A published capture may not exceed this size (base64 grows by 4/3).
MAX_PUBLISH_BYTES = 4 * 1024 * 1024
#: The whole answer (base64 + metadata) must stay below the 8 MiB bridge frame.
MAX_FRAME_BUDGET_BYTES = int(7.5 * 1024 * 1024)
#: PNG file signature.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: An armed session older than this is refused and rolled back by the janitor.
SESSION_TTL_SECONDS = 45.0
#: A draw handler left behind by a dead client is removed after this long.
PROBE_TTL_SECONDS = 20.0
#: Bounded evidence kept per session.
MAX_PROBE_OBSERVATIONS = 4
#: Bounded per-space scalar properties snapshotted/restored.
MAX_SPACE_FIELDS = 48
#: Bounded instance vertices gathered for one inspected object.
MAX_INSTANCE_POINTS = 200_000

#: Live capture sessions, keyed by session id.  Only touched on Blender's main
#: thread (every bridge command and every timer callback runs there).
_SESSIONS: dict[str, dict] = {}
_JANITOR = {"registered": False}


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
def fail(api, code, message, details=None):
    """Raise the add-on's structured command error."""
    raise api.CommandError(code, message, details=details)


def pointer(owner):
    try:
        return int(owner.as_pointer())
    except Exception:  # noqa: BLE001 - a real RNA block always has one
        return None


def main_thread_only_name(owner):
    try:
        return getattr(owner, "name", None)
    except Exception:  # noqa: BLE001
        return None


def is_background() -> bool:
    return bool(getattr(bpy.app, "background", False))


def require_gui(api, what):
    """Screenshots need a real GUI session; ``--background`` fails closed."""
    if is_background():
        fail(
            api,
            "gui_required",
            "%s needs an interactive GUI Blender: bpy.ops.screen.screenshot_area reads the window "
            "framebuffer, which does not exist in --background" % what,
            details={"background": True},
        )


# --------------------------------------------------------------------------- #
# windows / areas / regions
# --------------------------------------------------------------------------- #
def window_list() -> list:
    """Every window of every window manager, in Blender's own order."""
    windows = []
    try:
        managers = list(bpy.data.window_managers)
    except Exception:  # noqa: BLE001
        return windows
    for manager in managers:
        try:
            windows.extend(list(manager.windows))
        except Exception:  # noqa: BLE001
            continue
    return windows


def window_report() -> list:
    report = []
    for index, window in enumerate(window_list()):
        try:
            scene = window.scene.name if window.scene else None
            layer = window.view_layer.name if window.view_layer else None
            areas = [(i, a.type) for i, a in enumerate(list(window.screen.areas))] if window.screen else []
        except Exception:  # noqa: BLE001
            scene, layer, areas = None, None, []
        report.append({"index": index, "scene": scene, "view_layer": layer, "areas": areas,
                       "is_context_window": window is getattr(bpy.context, "window", None)})
    return report


def resolve_window(api, window_index):
    """The requested window by exact index, else the context/first window."""
    windows = window_list()
    if not windows:
        fail(api, "context_unavailable",
             "this Blender session exposes no window at all, so no editor can be captured",
             details={"background": is_background()})
    if window_index is None:
        context = getattr(bpy.context, "window", None)
        for index, window in enumerate(windows):
            if window is context:
                return window, index
        return windows[0], 0
    if isinstance(window_index, bool) or not isinstance(window_index, int):
        fail(api, "invalid_argument", "window_index must be an integer", details={"window_index": window_index})
    if window_index < 0 or window_index >= len(windows):
        fail(api, "window_index_out_of_range",
             "window_index %d does not exist (this session has %d window(s))" % (window_index, len(windows)),
             details={"windows": window_report()})
    return windows[window_index], window_index


def area_list(window) -> list:
    try:
        return list(window.screen.areas) if window.screen else []
    except Exception:  # noqa: BLE001
        return []


def area_report(window) -> list:
    report = []
    for index, area in enumerate(area_list(window)):
        try:
            spaces = [getattr(space, "type", None) for space in area.spaces]
            tree = None
            if getattr(area, "type", None) == "NODE_EDITOR":
                for space in area.spaces:
                    if getattr(space, "type", None) == "NODE_EDITOR":
                        tree = main_thread_only_name(getattr(space, "node_tree", None))
            report.append({"index": index, "type": getattr(area, "type", None),
                           "width": int(getattr(area, "width", 0) or 0),
                           "height": int(getattr(area, "height", 0) or 0),
                           "spaces": spaces, "node_tree": tree})
        except Exception:  # noqa: BLE001
            continue
    return report


def resolve_area(api, window, area_index, predicate, label):
    """Area by exact index or the largest matching area for readable captures.

    Returns ``None`` when no index was given and no area matches, so the caller
    can decide whether temporarily reconfiguring an area is allowed.
    """
    areas = area_list(window)
    if area_index is not None:
        if isinstance(area_index, bool) or not isinstance(area_index, int):
            fail(api, "invalid_argument", "area_index must be an integer", details={"area_index": area_index})
        if area_index < 0 or area_index >= len(areas):
            fail(api, "area_index_out_of_range",
                 "area_index %d does not exist in the selected window (%d area(s))"
                 % (area_index, len(areas)),
                 details={"areas": area_report(window), "windows": window_report()})
        area = areas[area_index]
        if not predicate(area):
            fail(api, "wrong_area_type",
                 "area %d is %r, not the %s this tool needs; pass a matching area_index or omit it"
                 % (area_index, getattr(area, "type", None), label),
                 details={"areas": area_report(window), "requested_index": area_index})
        return area, area_index
    candidates = [(area, index) for index, area in enumerate(areas) if predicate(area)]
    if candidates:
        return max(candidates, key=lambda item: int(getattr(item[0], "width", 0) or 0)
                   * int(getattr(item[0], "height", 0) or 0))
    return None, None


def region_of(area, region_type="WINDOW"):
    try:
        for region in area.regions:
            if getattr(region, "type", None) == region_type:
                return region
    except Exception:  # noqa: BLE001
        return None
    return None


def require_region(api, area):
    region = region_of(area, "WINDOW")
    if region is None or int(getattr(region, "width", 0) or 0) <= 0 or int(getattr(region, "height", 0) or 0) <= 0:
        fail(api, "region_unavailable",
             "the target area has no drawable WINDOW region (collapsed or not drawn yet)",
             details={"area_type": getattr(area, "type", None),
                      "regions": [(getattr(r, "type", None), int(getattr(r, "width", 0) or 0),
                                   int(getattr(r, "height", 0) or 0)) for r in getattr(area, "regions", [])]})
    return region


def space_of(area, space_type):
    try:
        for space in area.spaces:
            if getattr(space, "type", None) == space_type:
                return space
    except Exception:  # noqa: BLE001
        return None
    return None


def switch_window_scene(api, window, scene, report):
    """Temporarily point ``window.scene`` at ``scene`` and *prove* it stuck."""
    try:
        previous = window.scene
    except Exception as exc:  # noqa: BLE001
        fail(api, "context_unavailable", "the selected window reports no scene: %s" % exc)
    if previous is scene:
        return previous, {"changed": False, "previous_scene": main_thread_only_name(previous),
                          "window_scene": main_thread_only_name(scene), "verified": True}
    try:
        window.scene = scene
    except Exception as exc:  # noqa: BLE001
        report.setdefault("failures", []).append("window.scene assignment failed: %s" % exc)
        fail(api, "scene_switch_failed",
             "could not show scene %r in the selected window: %s" % (main_thread_only_name(scene), exc),
             details={"previous_scene": main_thread_only_name(previous)})
    try:
        current = window.scene
    except Exception as exc:  # noqa: BLE001
        current = None
        report.setdefault("failures", []).append("window.scene read-back failed: %s" % exc)
    if current is not scene:
        try:
            window.scene = previous
        except Exception as exc:  # noqa: BLE001
            report.setdefault("failures", []).append("rolling window.scene back failed: %s" % exc)
        fail(api, "scene_switch_unverified",
             "Blender did not keep window.scene = %r (it reports %r), so the requested scene cannot be "
             "shown; the previous scene was restored"
             % (main_thread_only_name(scene), main_thread_only_name(current)),
             details={"previous_scene": main_thread_only_name(previous)})
    return previous, {"changed": True, "previous_scene": main_thread_only_name(previous),
                      "window_scene": main_thread_only_name(scene), "verified": True}


def restore_window_scene(window, previous, report):
    if previous is None:
        return {"attempted": False, "restored": False, "reason": "no previous scene recorded"}
    try:
        window.scene = previous
        restored = window.scene is previous
    except Exception as exc:  # noqa: BLE001
        report.setdefault("failures", []).append("window.scene restore failed: %s" % exc)
        return {"attempted": True, "restored": False, "previous_scene": main_thread_only_name(previous),
                "error": "%s: %s" % (type(exc).__name__, exc)}
    if not restored:
        report.setdefault("failures", []).append("window.scene restore did not stick")
    return {"attempted": True, "restored": bool(restored), "previous_scene": main_thread_only_name(previous),
            "window_scene": main_thread_only_name(getattr(window, "scene", None))}


# --------------------------------------------------------------------------- #
# paths, evidence, atomic publication
# --------------------------------------------------------------------------- #
def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def project_dir(api) -> Path:
    getter = getattr(api, "project_dir", None)
    if callable(getter):
        return Path(getter())
    return Path(str(getattr(api, "PROJECT_DIR", Path.cwd())))


def _local_resolve(api, relative: str) -> Path:
    """Mirror of the add-on ``resolve_project_path`` for standalone module tests."""
    root = project_dir(api).resolve()
    raw = relative.replace("/", os.sep)
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        fail(api, "invalid_path", "path must be relative to <workspace>/Project")
    parts = [part for part in raw.split(os.sep) if part not in ("", ".")]
    if any(part == ".." for part in parts):
        fail(api, "invalid_path", "path traversal ('..') is rejected")
    if not parts:
        fail(api, "invalid_path", "path does not name a file")
    walk = root
    for part in parts[:-1]:
        walk = walk / part
        if walk.exists() and _is_reparse_point(walk):
            fail(api, "invalid_path", "symlinked/junction directories are rejected",
                 details={"component": str(walk)})
    candidate = root.joinpath(*parts)
    if candidate.exists() and _is_reparse_point(candidate):
        fail(api, "invalid_path", "symlinked target files are rejected")
    real = Path(os.path.realpath(str(candidate)))
    if not _within(str(real), str(root)):
        fail(api, "path_outside_project", "resolved path escapes <workspace>/Project",
             details={"resolved": str(real), "project_root": str(root)})
    return real


def _within(child: str, parent: str) -> bool:
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def validate_publish_path(api, relative_path):
    """``previews/<name>.png`` -> ``(relative, absolute, previews_root)``."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        fail(api, "invalid_argument", "relative_path must be a non-empty string ending in .png")
    raw = relative_path.strip().replace("\\", "/")
    if not raw.lower().endswith(".png"):
        fail(api, "invalid_extension", "captures are PNG files; %r does not end in .png" % raw)
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        fail(api, "invalid_path", "relative_path must be relative to <workspace>/Project")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        fail(api, "invalid_path", "path traversal ('..') is rejected")
    if len(parts) < 2 or parts[0].lower() != PREVIEW_SUBDIR:
        fail(api, "invalid_path",
             "captures are published to <workspace>/Project/%s/*.png, got %r" % (PREVIEW_SUBDIR, raw))
    relative = "/".join(parts)
    resolver = getattr(api, "resolve_project_path", None)
    resolved = Path(resolver(relative)) if callable(resolver) else _local_resolve(api, relative)
    root = Path(os.path.realpath(str(project_dir(api) / PREVIEW_SUBDIR)))
    real = Path(os.path.realpath(str(resolved)))
    if real != root and root not in real.parents:
        fail(api, "invalid_path", "resolved capture path escapes %s" % root,
             details={"resolved": str(real), "previews_root": str(root)})
    return relative, real, root


def safe_name(value, limit=48):
    """A filesystem-safe, still readable fragment of an object/group name."""
    text = str(value or "").strip()
    keep = []
    for char in text:
        if char.isalnum() or char in ("-", "_", "."):
            keep.append(char)
        elif "\u4e00" <= char <= "\u9fff":
            keep.append(char)
        else:
            keep.append("_")
    cleaned = "".join(keep).strip("_.")
    return (cleaned[:limit] or "capture")


def default_relative_path(kind, name=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    tail = "-%s" % safe_name(name) if name else ""
    return "%s/%s%s-%s-%s.png" % (PREVIEW_SUBDIR, safe_name(kind, 24), tail, stamp, uuid.uuid4().hex[:6])


def runtime_tmp_dir(api) -> Path:
    getter = getattr(api, "runtime_dir", None)
    base = Path(getter()) if callable(getter) else Path(str(getattr(api, "RUNTIME_DIR", Path.cwd())))
    path = base / "tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(api, "capture_dir_unwritable", "cannot create the capture temp directory %s: %s" % (path, exc),
             details={"directory": str(path), "errno": getattr(exc, "errno", None)})
    return path


def temp_capture_path(api) -> Path:
    return runtime_tmp_dir(api) / ("capture-%s.png" % uuid.uuid4().hex[:12])


def png_evidence(api, path: Path) -> dict:
    """Read a PNG and report provable facts about the bytes on disk."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        fail(api, "capture_unreadable", "cannot read the capture file %s: %s" % (path, exc))
    if not raw.startswith(PNG_MAGIC):
        fail(api, "invalid_image", "Blender did not write a PNG to %s (signature missing)" % path,
             details={"path": str(path), "bytes": len(raw)})
    if len(raw) < 33:
        fail(api, "invalid_image", "the PNG at %s is truncated (%d bytes)" % (path, len(raw)))
    width, height = struct.unpack(">II", raw[16:24])
    for label, value in (("width", width), ("height", height)):
        if value < MIN_IMAGE_DIMENSION or value > MAX_IMAGE_DIMENSION:
            fail(api, "invalid_image",
                 "the captured image reports %s=%d pixels, outside the accepted %d..%d range"
                 % (label, value, MIN_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                 details={"path": str(path), "width": int(width), "height": int(height)})
    stat_result = Path(path).stat()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "path": str(path),
        "bytes": len(raw),
        "width": int(width),
        "height": int(height),
        "signature": PNG_MAGIC.hex(),
        "sha256": digest,
        "sha256_prefix": digest[:32],
        "mtime_ns": int(stat_result.st_mtime_ns),
        "mtime": float(stat_result.st_mtime),
    }


def publish_capture(api, temp_path: Path, target: Path, relative: str, overwrite: bool) -> dict:
    """Atomically publish a freshly captured PNG and re-verify the result."""
    temp_path, target = Path(temp_path), Path(target)
    if target.exists() and not overwrite:
        fail(api, "file_exists", "%s already exists; pass overwrite=true to replace it" % target,
             details={"path": str(target), "relative_path": relative})
    evidence = png_evidence(api, temp_path)
    if evidence["bytes"] > MAX_PUBLISH_BYTES:
        fail(api, "capture_too_large",
             "the captured PNG is %.2f MiB, above the %.2f MiB cap; capture a smaller window/area or "
             "reduce the Blender window size" % (evidence["bytes"] / 1048576.0, MAX_PUBLISH_BYTES / 1048576.0),
             details={"bytes": evidence["bytes"], "max_bytes": MAX_PUBLISH_BYTES,
                      "width": evidence["width"], "height": evidence["height"]})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(api, "capture_dir_unwritable", "cannot create %s: %s" % (target.parent, exc),
             details={"directory": str(target.parent), "errno": getattr(exc, "errno", None)})
    try:
        os.replace(str(temp_path), str(target))
    except OSError as exc:
        fail(api, "capture_write_failed",
             "cannot publish the capture to %s: %s (an existing file, if any, was left untouched)"
             % (target, exc),
             details={"path": str(target), "errno": getattr(exc, "errno", None), "previous_kept": True})
    published = png_evidence(api, target)
    if published["sha256"] != evidence["sha256"] or published["bytes"] != evidence["bytes"]:
        fail(api, "publish_verify_failed",
             "the published file %s does not match the captured bytes" % target,
             details={"captured": evidence, "published": published})
    published["relative_path"] = relative
    return published


def cleanup(path) -> bool:
    try:
        if path and Path(path).exists():
            Path(path).unlink()
            return True
    except OSError:
        return False
    return False


def guard_payload_size(api, raw_bytes: bytes, metadata: dict):
    """Refuse an answer that could not fit the 8 MiB bridge frame."""
    b64_len = 4 * (len(raw_bytes) + 2) // 3
    try:
        meta_len = len(json.dumps(metadata, default=str))
    except (TypeError, ValueError):
        meta_len = 0
    total = b64_len + meta_len
    if total > MAX_FRAME_BUDGET_BYTES:
        fail(api, "payload_too_large",
             "the capture plus metadata would need about %.2f MiB, above the %.2f MiB bridge frame "
             "budget; pass a smaller window/area or narrow 'nodes'" % (total / 1048576.0,
                                                                      MAX_FRAME_BUDGET_BYTES / 1048576.0),
             details={"estimated_bytes": total, "budget_bytes": MAX_FRAME_BUDGET_BYTES,
                      "image_bytes": len(raw_bytes), "metadata_bytes": meta_len})


def capture_payload(api, published: dict, relative: str, start_epoch: float, extra: dict) -> dict:
    """Metadata + base64 for one published capture (the MCP image payload)."""
    path = Path(published["path"])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        fail(api, "capture_unreadable", "cannot read back the published capture: %s" % exc)
    metadata = {
        "path": str(path),
        "relative_path": relative,
        "bytes": published["bytes"],
        "size": {"width": published["width"], "height": published["height"]},
        "width": published["width"],
        "height": published["height"],
        "signature": published["signature"],
        "sha256": published["sha256"],
        "mtime_ns": published["mtime_ns"],
        "mtime": published["mtime"],
        "captured_at": round(time.time(), 3),
        "duration_ms": round((time.time() - start_epoch) * 1000.0, 3),
        "saved_blend": False,
        "image_source": (extra or {}).get("capture", {}).get("operator", "real editor framebuffer"),
        "limits": {
            "max_publish_bytes": MAX_PUBLISH_BYTES,
            "frame_budget_bytes": MAX_FRAME_BUDGET_BYTES,
            "min_dimension": MIN_IMAGE_DIMENSION,
            "max_dimension": MAX_IMAGE_DIMENSION,
        },
    }
    metadata.update(extra or {})
    if published["bytes"] < 4096 and published["width"] * published["height"] > 65536:
        metadata.setdefault("warnings", []).append(
            "the PNG is unusually small for its pixel size (%d bytes); a covered/minimised Blender window "
            "can capture a blank frame" % published["bytes"])
    guard_payload_size(api, raw, metadata)
    metadata["image_base64"] = base64.b64encode(raw).decode("ascii")
    return metadata


# --------------------------------------------------------------------------- #
# UI state: snapshot / change / restore
# --------------------------------------------------------------------------- #
_PRIMITIVE_TYPES = {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}


def capture_fields(owner, limit=MAX_SPACE_FIELDS, skip=("rna_type",)) -> dict:
    """Bounded snapshot of an RNA owner's writable scalar properties."""
    fields = {}
    if owner is None:
        return fields
    try:
        properties = owner.bl_rna.properties
    except Exception:  # noqa: BLE001
        return fields
    for prop in properties:
        if len(fields) >= limit:
            break
        try:
            identifier = prop.identifier
            if identifier in skip or prop.type not in _PRIMITIVE_TYPES or prop.is_readonly:
                continue
            value = getattr(owner, identifier)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, bool) or isinstance(value, (int, float, str)):
            fields[identifier] = value
    return fields


def restore_fields(owner, fields: dict, report: dict, label: str) -> list:
    """Restore only the properties that actually differ; report failures."""
    restored = []
    if owner is None or not fields:
        return restored
    for key, value in fields.items():
        try:
            if getattr(owner, key) == value:
                continue
            setattr(owner, key, value)
            restored.append(key)
        except Exception as exc:  # noqa: BLE001
            report.setdefault("failures", []).append("%s.%s restore failed: %s" % (label, key, exc))
    return restored


def snapshot_region_3d(space) -> dict:
    region_3d = getattr(space, "region_3d", None)
    if region_3d is None:
        return None
    try:
        return {
            "pointer": pointer(region_3d),
            "view_location": [float(value) for value in region_3d.view_location],
            "view_rotation": [float(value) for value in region_3d.view_rotation],
            "view_distance": float(region_3d.view_distance),
            "view_perspective": str(region_3d.view_perspective),
        }
    except Exception:  # noqa: BLE001
        return None


def restore_region_3d(space, snapshot: dict, report: dict, label: str) -> dict:
    if not snapshot:
        return {"available": False, "restored": False, "reason": "region_3d was not readable"}
    region_3d = getattr(space, "region_3d", None)
    if region_3d is None:
        return {"available": False, "restored": False, "reason": "region_3d is not exposed by this space"}
    changes = []
    try:
        if list(region_3d.view_rotation) != list(snapshot["view_rotation"]):
            region_3d.view_rotation = snapshot["view_rotation"]
            changes.append("view_rotation")
        if list(region_3d.view_location) != list(snapshot["view_location"]):
            region_3d.view_location = snapshot["view_location"]
            changes.append("view_location")
        if float(region_3d.view_distance) != float(snapshot["view_distance"]):
            region_3d.view_distance = snapshot["view_distance"]
            changes.append("view_distance")
        if str(region_3d.view_perspective) != str(snapshot["view_perspective"]):
            region_3d.view_perspective = snapshot["view_perspective"]
            changes.append("view_perspective")
    except Exception as exc:  # noqa: BLE001
        report.setdefault("failures", []).append("region_3d restore failed: %s" % exc)
        return {"available": True, "restored": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "changes": changes}
    return {"available": True, "restored": True, "changes": changes}


def snapshot_node_editor(space) -> dict:
    tree = getattr(space, "node_tree", None)
    path = []
    try:
        for index, item in enumerate(getattr(space, "path", []) or []):
            path.append({"node_tree": main_thread_only_name(getattr(item, "node_tree", None)),
                         "node_tree_pointer": pointer(getattr(item, "node_tree", None))})
    except Exception:  # noqa: BLE001
        path = []
    return {
        "node_tree": main_thread_only_name(tree),
        "node_tree_pointer": pointer(tree),
        "tree_type": getattr(space, "tree_type", None),
        "shader_type": getattr(space, "shader_type", None),
        "geometry_nodes_type": getattr(space, "geometry_nodes_type", None),
        "pin": bool(getattr(space, "pin", False)),
        "id": main_thread_only_name(getattr(space, "id", None)),
        "id_pointer": pointer(getattr(space, "id", None)),
        "path": path,
        "show_region_ui": bool(getattr(space, "show_region_ui", False)),
    }


def snapshot_space(space) -> dict:
    return {
        "type": getattr(space, "type", None),
        "pointer": pointer(space),
        "fields": capture_fields(space),
        "overlay": ({"pointer": pointer(getattr(space, "overlay", None)),
                     "fields": capture_fields(getattr(space, "overlay", None), 16)}
                    if getattr(space, "overlay", None) is not None else None),
        "region_3d": snapshot_region_3d(space),
        "node_editor": snapshot_node_editor(space) if getattr(space, "type", None) == "NODE_EDITOR" else None,
    }


def snapshot_area(area) -> dict:
    return {
        "type": getattr(area, "type", None),
        "pointer": pointer(area),
        "width": int(getattr(area, "width", 0) or 0),
        "height": int(getattr(area, "height", 0) or 0),
        "spaces": [snapshot_space(space) for space in getattr(area, "spaces", [])],
    }


def show_node_tree(api, space, tree, expected_type="GeometryNodeTree") -> dict:
    """Force a node editor to display ``tree`` and report every step honestly."""
    report = {"requested": main_thread_only_name(tree), "requested_pointer": pointer(tree),
              "attempts": [], "displayed": None, "displayed_pointer": None, "verified": False}
    if tree is None:
        fail(api, "invalid_argument", "no node tree was resolved to display")
    try:
        if bool(getattr(space, "pin", False)):
            space.pin = False
            report["attempts"].append("pin=False")
    except Exception as exc:  # noqa: BLE001
        report["attempts"].append("pin=False failed: %s" % exc)
    if expected_type:
        try:
            if getattr(space, "tree_type", None) != expected_type:
                space.tree_type = expected_type
                report["attempts"].append("tree_type=%s" % expected_type)
        except Exception as exc:  # noqa: BLE001
            report["attempts"].append("tree_type=%s failed: %s" % (expected_type, exc))
    # Leaving a nested group view: the RNA path is the reliable switch.
    path = getattr(space, "path", None)
    cleared = False
    if path is not None:
        try:
            length = len(path)
        except Exception:  # noqa: BLE001
            length = 0
        if length:
            clearer = getattr(path, "clear", None)
            if callable(clearer):
                try:
                    clearer()
                    cleared = True
                    report["attempts"].append("path.clear()")
                except Exception as exc:  # noqa: BLE001
                    report["attempts"].append("path.clear() failed: %s" % exc)
            if not cleared:
                for _ in range(16):
                    try:
                        if len(path) == 0:
                            break
                    except Exception:  # noqa: BLE001
                        break
                    try:
                        bpy.ops.node.tree_path_parent()
                        report["attempts"].append("tree_path_parent")
                    except Exception as exc:  # noqa: BLE001
                        report["attempts"].append("tree_path_parent failed: %s" % exc)
                        break
    try:
        if pointer(getattr(space, "node_tree", None)) != pointer(tree):
            space.node_tree = tree
            report["attempts"].append("node_tree=%s" % main_thread_only_name(tree))
        space.pin = True
    except Exception as exc:  # noqa: BLE001
        report["attempts"].append("node_tree assignment failed: %s" % exc)
    displayed = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    if pointer(displayed) != pointer(tree) and callable(getattr(path, "start", None)):
        # Space.node_tree polls modifier/tool asset traits in 5.2. The path API
        # also displays ordinary nested/non-asset trees without changing them.
        try:
            space.pin = True
            path.start(tree)
            report["attempts"].append("path.start(%s)" % main_thread_only_name(tree))
        except Exception as exc:  # noqa: BLE001
            report["attempts"].append("path.start failed: %s" % exc)
        displayed = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    report["displayed"] = main_thread_only_name(displayed)
    report["displayed_pointer"] = pointer(displayed)
    report["displayed_tree_type"] = getattr(space, "tree_type", None)
    report["verified"] = pointer(displayed) == pointer(tree) and (
        not expected_type or getattr(space, "tree_type", None) in (None, expected_type)
    )
    if not report["verified"]:
        fail(api, "tree_not_displayed",
             "the node editor does not show the requested node group %r (it reports %r, tree_type %r); "
             "no screenshot was taken"
             % (main_thread_only_name(tree), report["displayed"], report.get("displayed_tree_type")),
             details={"display": report, "areas": "pass a window_index/area_index of a Geometry Nodes "
                                                  "editor, or object/modifier so the editor can be pinned"})
    return report


def restore_space(space, snapshot: dict, report: dict, tree_lookup=None) -> dict:
    """Restore one space; returns what was restored and what failed."""
    result = {"space_type": snapshot.get("type"), "fields_restored": [], "overlay_restored": [],
              "region_3d": None, "node_tree": None}
    if space is None:
        result["error"] = "the original space no longer exists"
        report.setdefault("failures", []).append("the original space is gone; its state could not be restored")
        return result
    node_snapshot = snapshot.get("node_editor")
    if node_snapshot is not None and tree_lookup is not None:
        # Match the original editor type before assigning a Shader/Geometry tree.
        result["fields_restored"] = restore_fields(space, snapshot.get("fields") or {}, report, "space")
        # ``None`` is a real state: an editor that had no node tree must be cleared
        # again, otherwise the borrowed tree would reappear the next time the area
        # is switched back to a node editor.
        wanted = tree_lookup(node_snapshot)
        current = getattr(space, "node_tree", None)
        if current is wanted:
            result["node_tree"] = {"assigned": main_thread_only_name(wanted), "already_current": True}
        else:
            try:
                space.node_tree = wanted
                result["node_tree"] = {"assigned": main_thread_only_name(wanted),
                                       "cleared": wanted is None}
            except Exception as exc:  # noqa: BLE001
                result["node_tree"] = {"assigned": None, "error": "%s: %s" % (type(exc).__name__, exc)}
                report.setdefault("failures", []).append("node_tree restore failed: %s" % exc)
        path = getattr(space, "path", None)
        wanted_path = node_snapshot.get("path") or []
        actual_path = [pointer(getattr(item, "node_tree", None)) for item in path or []]
        if actual_path != [entry.get("node_tree_pointer") for entry in wanted_path]:
            try:
                if path is None:
                    raise RuntimeError("node editor path is unavailable")
                trees = [tree_lookup(entry) for entry in wanted_path]
                if any(tree is None for tree in trees):
                    raise RuntimeError("an original node tree path entry is unavailable")
                path.clear()
                if trees:
                    path.start(trees[0])
                    for tree in trees[1:]:
                        path.append(tree)
                if [pointer(getattr(item, "node_tree", None)) for item in path] != [
                        entry.get("node_tree_pointer") for entry in wanted_path]:
                    raise RuntimeError("node editor path restoration read-back mismatch")
                result["node_path_restored"] = True
            except Exception as exc:  # noqa: BLE001
                result["node_path_restored"] = False
                report.setdefault("failures", []).append("node editor path restore failed: %s" % exc)
    if node_snapshot is None or tree_lookup is None:
        result["fields_restored"] = restore_fields(space, snapshot.get("fields") or {}, report, "space")
    overlay_snapshot = snapshot.get("overlay")
    if overlay_snapshot:
        result["overlay_restored"] = restore_fields(getattr(space, "overlay", None),
                                                    overlay_snapshot.get("fields") or {}, report, "overlay")
    result["region_3d"] = restore_region_3d(space, snapshot.get("region_3d"), report, "region_3d")
    return result


def restore_area(area, snapshot: dict, report: dict, tree_lookup=None) -> dict:
    """Restore area type *and* the per-space state of the area we borrowed."""
    result = {"area_type": {"before": getattr(area, "type", None), "wanted": snapshot.get("type"),
                            "restored": False}, "spaces": [], "space_reused": None}
    if area is None:
        result["error"] = "the borrowed area no longer exists"
        report.setdefault("failures", []).append("the borrowed area is gone; its type could not be restored")
        return result
    wanted_type = snapshot.get("type")
    if wanted_type and getattr(area, "type", None) != wanted_type:
        try:
            area.type = wanted_type
            result["area_type"]["restored"] = getattr(area, "type", None) == wanted_type
        except Exception as exc:  # noqa: BLE001
            result["area_type"]["error"] = "%s: %s" % (type(exc).__name__, exc)
            report.setdefault("failures", []).append("area.type restore failed: %s" % exc)
    else:
        result["area_type"]["restored"] = True
    wanted_spaces = snapshot.get("spaces") or []
    current_spaces = list(getattr(area, "spaces", []))
    for index, space_snapshot in enumerate(wanted_spaces):
        wanted_pointer = space_snapshot.get("pointer")
        space = None
        for candidate in current_spaces:
            if pointer(candidate) == wanted_pointer:
                space = candidate
                break
        if space is not None:
            result["space_reused"] = True
        else:
            for candidate in current_spaces:
                if getattr(candidate, "type", None) == space_snapshot.get("type"):
                    space = candidate
                    break
            if space is not None:
                result["space_reused"] = False
        result["spaces"].append(restore_space(space, space_snapshot, report, tree_lookup))
    return result


def snapshot_selection(window) -> dict:
    layer = getattr(window, "view_layer", None)
    snapshot = {"selected": [], "active": None, "layer": main_thread_only_name(layer)}
    if layer is None:
        return snapshot
    try:
        snapshot["selected"] = sorted(obj.name for obj in bpy.context.selected_objects)
    except Exception:  # noqa: BLE001
        snapshot["selected"] = []
    try:
        active = layer.objects.active
        snapshot["active"] = main_thread_only_name(active)
    except Exception:  # noqa: BLE001
        snapshot["active"] = None
    return snapshot


def restore_selection(window, snapshot: dict, report: dict) -> dict:
    layer = getattr(window, "view_layer", None)
    result = {"restored": False, "changes": [], "failures": []}
    if layer is None or not snapshot:
        result["reason"] = "no view layer or no snapshot"
        return result
    wanted = set(snapshot.get("selected") or [])
    try:
        for name in wanted:
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            try:
                if not obj.select_get(view_layer=layer):
                    obj.select_set(True, view_layer=layer)
                    result["changes"].append("select:" + name)
            except Exception as exc:  # noqa: BLE001
                result["failures"].append("select %s: %s" % (name, exc))
        for obj in list(layer.objects):
            name = getattr(obj, "name", None)
            if name in wanted:
                continue
            try:
                if obj.select_get(view_layer=layer):
                    obj.select_set(False, view_layer=layer)
                    result["changes"].append("deselect:" + str(name))
            except Exception as exc:  # noqa: BLE001
                result["failures"].append("deselect %s: %s" % (name, exc))
        active_name = snapshot.get("active")
        active = bpy.data.objects.get(active_name) if active_name else None
        if layer.objects.active is not active:
            layer.objects.active = active
            result["changes"].append("active:%s" % active_name)
    except Exception as exc:  # noqa: BLE001
        result["failures"].append("selection restore failed: %s" % exc)
    result["restored"] = not result["failures"]
    for failure in result["failures"]:
        report.setdefault("failures", []).append(failure)
    return result


# --------------------------------------------------------------------------- #
# draw probe: proof that the requested editor state reached the screen
# --------------------------------------------------------------------------- #
def _probe_draw(session: dict) -> None:
    """Draw handler: count frames and read the *drawn* space state."""
    try:
        session["draws"] = int(session.get("draws", 0)) + 1
        space = session.get("space")
        window = session.get("window")
        area = session.get("area")
        region = session.get("region")
        # Space draw callbacks are shared by ALL areas of this editor type.
        # Only the requested area's actual drawing context is evidence.
        if (pointer(getattr(bpy.context, "area", None)) != pointer(area)
                or pointer(getattr(bpy.context, "window", None)) != pointer(window)
                or pointer(getattr(bpy.context, "region", None)) != pointer(region)):
            return
        if getattr(bpy.context, "area", None) is None:
            return
        tree = (getattr(space, "edit_tree", getattr(space, "node_tree", None))
                if space is not None else None)
        scene = getattr(window, "scene", None) if window is not None else None
        observation = {
            "at": round(time.time(), 3),
            "space_type": getattr(space, "type", None),
            "tree": main_thread_only_name(tree),
            "tree_pointer": pointer(tree),
            "tree_type": getattr(space, "tree_type", None),
            "shader_type": getattr(space, "shader_type", None),
            "geometry_nodes_type": getattr(space, "geometry_nodes_type", None),
            "pin": bool(getattr(space, "pin", False)),
            "area_type": getattr(area, "type", None),
            "window_scene": main_thread_only_name(scene),
            "region": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
            "show_region_ui": bool(getattr(space, "show_region_ui", False)),
        }
        session["last"] = observation
        observations = session.setdefault("observations", [])
        if len(observations) < MAX_PROBE_OBSERVATIONS:
            observations.append(observation)
    except Exception as exc:  # noqa: BLE001 - a draw callback must never raise
        session["probe_errors"] = int(session.get("probe_errors", 0)) + 1
        session["last_error"] = "%s: %s" % (type(exc).__name__, exc)


def probe_start(api, session: dict) -> dict:
    space = session.get("space")
    handler = getattr(space, "draw_handler_add", None)
    if not callable(handler):
        return {"available": False, "reason": "this space does not expose draw_handler_add"}
    try:
        session["handler"] = handler(_probe_draw, (session,), "WINDOW", "POST_PIXEL")
        session["handler_space"] = space
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "draw_handler_add failed: %s: %s" % (type(exc).__name__, exc)}
    return {"available": True}


def probe_stop(session: dict) -> dict:
    handler = session.pop("handler", None)
    space = session.pop("handler_space", None)
    if handler is None:
        return {"removed": False, "reason": "no handler was registered"}
    remover = getattr(space, "draw_handler_remove", None)
    if not callable(remover):
        return {"removed": False, "reason": "the space no longer exposes draw_handler_remove"}
    try:
        remover(handler, "WINDOW")
        return {"removed": True}
    except Exception as exc:  # noqa: BLE001
        return {"removed": False, "reason": "%s: %s" % (type(exc).__name__, exc)}


def probe_report(session: dict) -> dict:
    return {
        "available": session.get("handler") is not None,
        "draws": int(session.get("draws", 0)),
        "observations": list(session.get("observations") or []),
        "last": session.get("last"),
        "errors": int(session.get("probe_errors", 0)),
        "last_error": session.get("last_error"),
        "removed": session.get("probe_removed"),
    }


def observed_matches(observation: dict, expect: dict) -> dict:
    """Compare one draw-time observation with what the request demanded."""
    if not observation:
        return {"matches": False, "reason": "no draw was observed yet", "checks": {}}
    checks = {}
    if expect.get("draw_after_epoch"):
        checks["view_settled"] = float(observation.get("at", 0)) >= expect["draw_after_epoch"]
    if expect.get("space_type"):
        checks["space_type"] = observation.get("space_type") == expect["space_type"]
    if expect.get("tree_pointer") is not None:
        checks["tree_identity"] = observation.get("tree_pointer") == expect["tree_pointer"]
    if expect.get("tree_name"):
        checks["tree_name"] = observation.get("tree") == expect["tree_name"]
    if expect.get("tree_type"):
        checks["tree_type"] = observation.get("tree_type") in (None, expect["tree_type"])
    if expect.get("shader_type"):
        checks["shader_type"] = observation.get("shader_type") in (None, expect["shader_type"])
    if expect.get("window_scene"):
        checks["window_scene"] = observation.get("window_scene") == expect["window_scene"]
    if expect.get("area_type"):
        checks["area_type"] = observation.get("area_type") == expect["area_type"]
    if expect.get("region"):
        # A sidebar redraw may resize the main region. Match its actual
        # identity/target and require a drawable size, not an obsolete size.
        checks["region_size"] = all(value > 0 for value in observation.get("region", [0, 0]))
    return {"matches": all(checks.values()) and bool(checks), "checks": checks,
            "observed": observation, "expected": expect}


# --------------------------------------------------------------------------- #
# capture sessions (prepare -> status -> commit/abort)
# --------------------------------------------------------------------------- #
def _janitor():
    """Remove leaked draw handlers and roll expired sessions back."""
    now = time.time()
    remaining = 0
    for session_id in list(_SESSIONS):
        session = _SESSIONS.get(session_id)
        if session is None:
            continue
        age = now - float(session.get("created", now))
        try:
            if session.get("handler") is not None and age > PROBE_TTL_SECONDS:
                session["probe_removed"] = probe_stop(session)
            if age > SESSION_TTL_SECONDS:
                session["expired"] = True
                restore_session(session, reason="session_expired")
                _SESSIONS.pop(session_id, None)
                continue
        except Exception:  # noqa: BLE001 - a janitor must never raise into Blender
            _SESSIONS.pop(session_id, None)
            continue
        remaining += 1
    if not remaining:
        _JANITOR["registered"] = False
        return None
    return 1.0


def _schedule_janitor():
    if _JANITOR["registered"]:
        return
    register = getattr(bpy.app, "timers", None)
    register = getattr(register, "register", None)
    if not callable(register):
        return
    try:
        register(_janitor, first_interval=1.0)
        _JANITOR["registered"] = True
    except Exception:  # noqa: BLE001
        _JANITOR["registered"] = False


def snapshot_ui(window, area) -> dict:
    """Everything ``restore_session`` needs to undo a temporary UI change."""
    return {
        "window": {
            "index": next((index for index, candidate in enumerate(window_list()) if pointer(candidate) == pointer(window)), None),
            "scene": main_thread_only_name(getattr(window, "scene", None)),
            "view_layer": main_thread_only_name(getattr(window, "view_layer", None)),
        },
        "area": snapshot_area(area),
        "selection": snapshot_selection(window),
        "previous_scene": getattr(window, "scene", None),
        "blend_dirty": bool(getattr(bpy.data, "is_dirty", False)),
    }


def expire_area_sessions(api, area, reason="superseded_by_new_prepare"):
    """Roll back live sessions that borrowed the *same* area (never another one)."""
    expired = []
    for session_id in list(_SESSIONS):
        session = _SESSIONS.get(session_id)
        if session is None or pointer(session.get("area")) != pointer(area):
            continue
        _SESSIONS.pop(session_id, None)
        session["expired"] = True
        restore_session(session, reason=reason)
        expired.append(session_id)
    return expired


def arm_session(api, kind, window, window_index, screen, area, area_index, region, space,
                snapshot, expect, start_epoch, warnings, notes, tree=None, tree_lookup=None,
                extra_restore=None, metadata_provider=None) -> dict:
    """Register a prepared capture; returns the public ``prepare`` payload."""
    expire_area_sessions(api, area)
    session_id = uuid.uuid4().hex[:16]
    session = {
        "id": session_id,
        "kind": kind,
        "created": time.time(),
        "start_epoch": start_epoch,
        "window": window,
        "window_index": window_index,
        "screen": screen,
        "area": area,
        "area_index": area_index,
        "region": region,
        "space": space,
        "tree": tree,
        "tree_lookup": tree_lookup,
        "extra_restore": extra_restore,
        "metadata_provider": metadata_provider,
        "expect": dict(expect or {}),
        "snapshot": snapshot,
        "draws": 0,
        "observations": [],
        "probe_errors": 0,
        "warnings": list(warnings or []),
        "notes": list(notes or []),
        "restore": None,
        "closed": False,
        "committed": False,
        "expired": False,
    }
    session["probe"] = probe_start(api, session)
    _SESSIONS[session_id] = session
    _schedule_janitor()
    return {
        "session": session_id,
        "kind": kind,
        "armed": True,
        "window_index": window_index,
        "area_index": area_index,
        "area_type": getattr(area, "type", None),
        "window_scene": main_thread_only_name(getattr(window, "scene", None)),
        "region": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
        "expect": session["expect"],
        "probe": session["probe"],
        "draws_after_arm": 0,
        "expires_in_s": round(SESSION_TTL_SECONDS - (time.time() - session["created"]), 3),
        "snapshot": _snapshot_summary(snapshot),
        "warnings": session["warnings"],
        "notes": session["notes"],
        "deferred_capture": {
            "reason": "the bridge handler is synchronous and cannot pump the UI event loop, so the redraw "
                      "must happen in Blender's main loop between two bridge calls",
            "protocol": "poll <tool>_status until draws_after_arm > 0 (and match.matches is true), then "
                        "call <tool>_commit; call <tool>_abort if you stop instead",
            "temporary_ui": "area type, node tree/pin/path, sidebar, scene, selection and view are restored "
                            "by commit (and by abort), also when the capture fails",
        },
    }


def _snapshot_summary(snapshot) -> dict:
    if not snapshot:
        return {}
    return {
        "area": {"type": (snapshot.get("area") or {}).get("type"),
                 "width": (snapshot.get("area") or {}).get("width"),
                 "height": (snapshot.get("area") or {}).get("height")},
        "space_types": [space.get("type") for space in (snapshot.get("area") or {}).get("spaces", [])],
        "window_scene": (snapshot.get("window") or {}).get("scene"),
        "selection": snapshot.get("selection"),
    }


def get_session(api, session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        fail(api, "invalid_argument", "session must be the id returned by the prepare call")
    session = _SESSIONS.get(session_id)
    if session is None:
        fail(api, "unknown_session",
             "capture session %r is unknown or already finished (committed, aborted or expired); call the "
             "prepare command again" % session_id,
             details={"live_sessions": sorted(_SESSIONS)})
    return session


def status_payload(api, session_id) -> dict:
    session = get_session(api, session_id)
    probe = probe_report(session)
    match = observed_matches(probe.get("last"), session.get("expect") or {})
    if not match.get("matches") and (session.get("expect") or {}).get("draw_after_epoch"):
        session["area"].tag_redraw()
    live_area = None
    live_tree = None
    live_region = None
    try:
        live_area = getattr(session["area"], "type", None)
    except Exception:  # noqa: BLE001
        live_area = None
    try:
        live_tree = main_thread_only_name(getattr(session["space"], "node_tree", None))
    except Exception:  # noqa: BLE001
        live_tree = None
    try:
        live_region = [int(session["region"].width), int(session["region"].height)]
    except Exception:  # noqa: BLE001
        live_region = None
    age = time.time() - float(session.get("created", time.time()))
    return {
        "session": session_id,
        "kind": session.get("kind"),
        "armed": not session["closed"],
        "committed": bool(session["committed"]),
        "expired": bool(session["expired"]),
        "age_s": round(age, 3),
        "expires_in_s": round(max(SESSION_TTL_SECONDS - age, 0.0), 3),
        "draws_after_arm": probe.get("draws", 0),
        "match": match,
        "probe": probe,
        "live": {"area_type": live_area, "tree": live_tree, "region": live_region},
        "expect": session.get("expect"),
        "warnings": session.get("warnings") or [],
    }


def require_drawn(api, session) -> dict:
    """A commit is only allowed after a draw showing the requested state."""
    probe = probe_report(session)
    if probe.get("draws", 0) <= 0:
        fail(api, "stale_frame",
             "no frame was drawn since the capture was prepared, so no screenshot was taken: the Blender "
             "window is probably minimised, covered or not redrawing. Bring the window to the front "
             "(without automating input) and retry",
             details={"probe": probe, "hint": "the front buffer would have been stale or blank"})
    match = observed_matches(probe.get("last"), session.get("expect") or {})
    if not match.get("matches"):
        fail(api, "target_not_drawn",
             "the drawn frame does not show the requested target (%s); refusing to return a screenshot of "
             "another editor or an empty viewport"
             % ", ".join(sorted(key for key, value in (match.get("checks") or {}).items() if not value)),
             details={"match": match, "probe": probe})
    return {"probe": probe, "match": match}


def commit_capture(api, session_id, relative_path, overwrite, extra=None) -> dict:
    """Screenshot the armed area, publish it, restore the UI, return the payload."""
    session = get_session(api, session_id)
    if session["committed"]:
        fail(api, "session_already_committed",
             "capture session %r was already committed" % session_id)
    if session["expired"]:
        fail(api, "session_expired", "capture session %r expired before it was committed" % session_id)
    relative, target, _root = validate_publish_path(api, relative_path)
    if target.exists() and not overwrite:
        fail(api, "file_exists", "%s already exists; pass overwrite=true to replace it" % target,
             details={"path": str(target), "relative_path": relative})
    evidence = require_drawn(api, session)

    window, area, region, space = session["window"], session["area"], session["region"], session["space"]
    areas = area_list(window)
    if not any(pointer(candidate) == pointer(area) for candidate in areas):
        fail(api, "area_gone", "the prepared editor area no longer exists in this window",
             details={"areas": area_report(window)})
    if getattr(area, "type", None) != session["expect"].get("area_type"):
        fail(api, "area_type_changed",
             "the prepared area changed type to %r after the capture was armed"
             % getattr(area, "type", None),
             details={"expected": session["expect"].get("area_type"),
                      "area_type": getattr(area, "type", None)})
    live_region = None
    try:
        live_region = [int(region.width), int(region.height)]
    except Exception:  # noqa: BLE001
        live_region = None
    if not live_region or live_region[0] <= 0 or live_region[1] <= 0:
        fail(api, "region_unavailable", "the prepared editor region has no drawable size any more",
             details={"region": live_region})

    temp_path = temp_capture_path(api)
    try:
        screenshot = screenshot_area(api, window, session["screen"], area, region, temp_path)
        # Module-specific evidence (node layout / evaluated model summary) is read
        # *after* the confirmed draw and *before* the UI is restored.  A provider
        # that fails refuses publication: required structural evidence must not
        # silently disappear from an otherwise successful image response.
        provided = {}
        provider = session.get("metadata_provider")
        if callable(provider):
            try:
                provided = provider() or {}
            except api.CommandError as exc:
                fail(api, "capture_metadata_failed", "capture evidence could not be collected: %s" % exc,
                     details={"cause_code": getattr(exc, "code", "command_error"), "published": False})
            except Exception as exc:  # noqa: BLE001
                fail(api, "capture_metadata_failed", "capture evidence could not be collected: %s" % exc,
                     details={"cause_type": type(exc).__name__, "published": False})
        published = publish_capture(api, temp_path, target, relative, overwrite)
        # Read the capture-time UI state *before* the restore runs.
        ui_state = {
            "window_index": session.get("window_index"),
            "area_index": session.get("area_index"),
            "area_type": screenshot.get("area_type"),
            "window_scene": main_thread_only_name(getattr(window, "scene", None)),
            "tree": main_thread_only_name(getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)),
            "tree_pointer": pointer(getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)),
            "sidebar_shown": bool(getattr(space, "show_region_ui", False)),
            "temporary": True,
        }
        restore_info = restore_session(session, reason="committed")
        warnings = list(session.get("warnings") or [])
        warnings.extend(screenshot.pop("warnings", []) or [])
        if live_region != (session["expect"].get("region") or live_region):
            warnings.append(
                "the editor was resized between prepare and commit (armed %s, now %s); the screenshot is "
                "the current size" % (session["expect"].get("region"), live_region))
        extra_metadata = {
            "session": session_id,
            "kind": session.get("kind"),
            "capture": screenshot,
            "draw_evidence": {"probe": evidence["probe"], "match": evidence["match"]},
            "ui_state": ui_state,
            "snapshot": _snapshot_summary(session.get("snapshot")),
            "warnings": warnings,
            "notes": list(session.get("notes") or []),
            "blend_dirty_before": bool((session.get("snapshot") or {}).get("blend_dirty", False)),
            "blend_dirty_after": bool(getattr(bpy.data, "is_dirty", False)),
        }
        extra_metadata.update(provided)
        extra_metadata.update(extra or {})
        restored = bool(restore_info.get("restored"))
        extra_metadata["restored"] = restored
        if not restored:
            extra_metadata.setdefault("warnings", []).append(
                "the temporary UI change could not be fully restored: %s"
                % "; ".join(restore_info.get("failures") or ["unknown reason"]))
        return capture_payload(api, published, relative,
                               float(session.get("start_epoch") or time.time()), extra_metadata)
    finally:
        cleanup(temp_path)
        if session.get("restore") is None:
            restore_session(session, reason="capture_failed")
        session["closed"] = True
        _SESSIONS.pop(session_id, None)


def screenshot_area(api, window, screen, area, region, target: Path) -> dict:
    """The real ``bpy.ops.screen.screenshot_area`` capture of one editor area."""
    require_gui(api, "capturing an editor area")
    capture = getattr(window, "screenshot", None)
    if callable(capture):
        # Blender 5.2: direct framebuffer pixels of an exact rectangle.
        # imbuf uses the same bottom-to-top convention as Window.screenshot.
        rectangle = ((int(area.x), int(area.y)),
                     (int(area.x + area.width), int(area.y + area.height)))
        try:
            import imbuf
            pixels = capture(region=rectangle, use_alpha=False)
            height, width = pixels.shape[:2]
            if (width, height) != (int(area.width), int(area.height)):
                raise ValueError("framebuffer size does not match the requested editor")
            flat = pixels.cast('B')
            first = bytes(flat[:4])
            step = max(4, ((len(flat) // 8192) // 4) * 4)
            if all(bytes(flat[i:i+4]) == first for i in range(0, len(flat)-3, step)):
                raise ValueError("uniform framebuffer; refusing a blank image")
            buffer = imbuf.new((width, height))
            try:
                buffer.file_type = "PNG"
                with buffer.with_buffer(write=True) as raw:
                    raw.cast('B')[:] = pixels.cast('B')
                imbuf.write(buffer, filepath=str(target))
            finally:
                buffer.free()
        except Exception as exc:
            fail(api, "screenshot_failed", "Window.screenshot failed: %s" % exc,
                 details={"rectangle": rectangle, "area_type": area.type})
        return {"operator": "bpy.types.Window.screenshot", "rectangle": rectangle,
                "area_type": area.type, "area_size": [width, height],
                "region_size": [region.width, region.height], "warnings": []}
    operator = getattr(getattr(bpy.ops, "screen", None), "screenshot_area", None)
    if operator is None:
        fail(api, "unsupported_api",
             "this Blender build exposes no bpy.ops.screen.screenshot_area; an editor screenshot cannot be "
             "produced without it (no render/camera fallback is used)")
    target = Path(target)
    kwargs = {"filepath": str(target)}
    properties = _operator_properties(operator)
    if "hide_ui" in properties:
        kwargs["hide_ui"] = False
    result = None
    error = None
    override = {"window": window, "screen": screen, "area": area, "region": region}
    try:
        with bpy.context.temp_override(**override):
            result = operator(**kwargs)
    except TypeError as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
        try:
            with bpy.context.temp_override(**override):
                result = operator(filepath=str(target))
        except Exception as retry_exc:  # noqa: BLE001
            fail(api, "screenshot_failed",
                 "bpy.ops.screen.screenshot_area failed: %s (retry without optional properties: %s)"
                 % (error, retry_exc), details={"area_type": getattr(area, "type", None)})
    except Exception as exc:  # noqa: BLE001
        fail(api, "screenshot_failed", "bpy.ops.screen.screenshot_area failed: %s" % exc,
             details={"area_type": getattr(area, "type", None),
                      "error_type": type(exc).__name__})
    outcome = set(result) if result is not None else set()
    if outcome and "FINISHED" not in outcome:
        fail(api, "screenshot_failed",
             "bpy.ops.screen.screenshot_area returned %s and wrote no image" % sorted(outcome),
             details={"result": sorted(outcome)})
    if not target.exists():
        fail(api, "screenshot_failed",
             "bpy.ops.screen.screenshot_area reported success but wrote no file to %s" % target,
             details={"result": sorted(outcome), "kwargs": sorted(kwargs)})
    return {
        "operator": "bpy.ops.screen.screenshot_area",
        "operator_result": sorted(outcome),
        "kwargs": sorted(kwargs),
        "area_type": getattr(area, "type", None),
        "area_size": [int(getattr(area, "width", 0) or 0), int(getattr(area, "height", 0) or 0)],
        "region_size": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
        "context_override": sorted(override),
        "hide_ui": bool(kwargs.get("hide_ui", False)),
        "warnings": [],
    }


def _operator_properties(operator) -> set:
    try:
        rna = operator.get_rna_type()
        return {prop.identifier for prop in rna.properties}
    except Exception:  # noqa: BLE001
        return set()


def restore_session(session, reason="aborted") -> dict:
    """Restore every temporary UI change the prepare step made."""
    if session.get("restore") is not None:
        return session["restore"]
    report = {"reason": reason, "failures": [], "attempted": True}
    snapshot = session.get("snapshot") or {}
    if session.get("handler") is not None:
        session["probe_removed"] = probe_stop(session)
    window = session.get("window")
    area = session.get("area")
    result = {"reason": reason, "failures": report["failures"], "area": None, "selection": None,
              "window_scene": None, "space": None, "tree_lookup": bool(session.get("tree_lookup_available"))}
    try:
        if area is not None:
            with bpy.context.temp_override(window=window, area=area):
                result["area"] = restore_area(area, snapshot.get("area") or {}, report,
                                              tree_lookup=session.get("tree_lookup"))
        if window is not None:
            result["selection"] = restore_selection(window, snapshot.get("selection") or {}, report)
            result["window_scene"] = restore_window_scene(window, snapshot.get("previous_scene"),
                                                          report)
        extra_restore = session.get("extra_restore")
        if callable(extra_restore):
            try:
                result["extra"] = extra_restore(report)
            except Exception as exc:  # noqa: BLE001
                report["failures"].append("extra restore failed: %s: %s" % (type(exc).__name__, exc))
    except Exception as exc:  # noqa: BLE001
        report["failures"].append("restore crashed: %s: %s" % (type(exc).__name__, exc))
    result["restored"] = not report["failures"]
    result["failures"] = report["failures"]
    session["restore"] = result
    session["closed"] = True
    return result


def abort_session(api, session_id) -> dict:
    session = _SESSIONS.pop(session_id, None)
    if session is None:
        return {"session": session_id, "aborted": False,
                "reason": "unknown, already committed, aborted or expired session (nothing to restore)"}
    session["expired"] = True
    restore = restore_session(session, reason="aborted")
    return {"session": session_id, "aborted": True, "restore": restore,
            "restored": bool(restore.get("restored"))}


def restore_ui(api, snapshot, window, area, reason="prepare_failed",
               tree_lookup=None, extra_restore=None) -> dict:
    """One-shot rollback for a prepare that failed before a session existed."""
    session = {
        "snapshot": snapshot,
        "window": window,
        "area": area,
        "restore": None,
        "tree_lookup": tree_lookup,
        "extra_restore": extra_restore,
        "handler": None,
        "probe_removed": None,
    }
    return restore_session(session, reason=reason)
