"""Model preview capture: a real image the caller (an LLM) can actually see.

``blender_capture_preview`` renders the current model and returns **native MCP
image content** (base64 PNG in an ``ImageContent`` block) next to a JSON metadata
block. The image is not merely a path: the model receives the bytes.

Where the file goes
-------------------
Everything is written inside ``<workspace>/Project/previews/*.png``. The path the
caller supplies is validated twice - here (``config.resolve_project_path`` plus a
``previews/`` requirement) and again inside Blender (the add-on re-implements the
same rules) - so an absolute path, a ``..`` escape, a symlink/junction component
or a non-PNG name is refused before anything is rendered.

How the image is produced
-------------------------
* ``mode="viewport"`` captures the real 3D viewport (``bpy.ops.render.opengl``)
  with a context override; it needs a GUI Blender and fails with an actionable
  message in ``--background``.
* ``mode="render"`` renders through a temporary camera framed on the *evaluated*
  model bounds (so Geometry Nodes output is what you see), using an isolated
  temporary scene that is removed again.
* ``mode="auto"`` uses the viewport when one is available and otherwise falls
  back to the camera render, and reports which one it actually used.

Render settings, the scene camera, the frame and every temporary datablock are
snapshotted and restored even when the capture fails - the tool never saves the
.blend and never leaves the user's scene modified.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from .. import config
from ..bridge import BridgeClient
from ._common import call

#: Hard bounds shared with the add-on side (which re-implements them).
MAX_PREVIEW_DIMENSION = 2048
MIN_PREVIEW_DIMENSION = 16
MAX_PREVIEW_PNG_BYTES = 3 * 1024 * 1024
PREVIEW_SUBDIR = "previews"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_TIMEOUT = 45.0
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PreviewError(Exception):
    """A capture that must be reported as an MCP tool error."""


def _previews_root() -> Path:
    return (config.project_dir() / PREVIEW_SUBDIR).resolve()


def _validate_dimensions(width: int, height: int) -> tuple[int, int]:
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise PreviewError("%s must be an integer, got %r" % (name, value))
        if value < MIN_PREVIEW_DIMENSION or value > MAX_PREVIEW_DIMENSION:
            raise PreviewError(
                "%s must be between %d and %d pixels (got %d)" % (name, MIN_PREVIEW_DIMENSION,
                                                                  MAX_PREVIEW_DIMENSION, value)
            )
    return int(width), int(height)


def default_relative_path() -> str:
    """A collision-free name inside ``Project/previews``."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return "%s/preview-%s-%s.png" % (PREVIEW_SUBDIR, stamp, uuid.uuid4().hex[:6])


def validate_relative_path(relative_path: str) -> tuple[str, Path]:
    """Return the normalised ``previews/...png`` relative path and its absolute target."""
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise PreviewError("path must not be empty")
    if not raw.lower().endswith(".png"):
        raise PreviewError("path must end with .png (PNG is the only supported preview format)")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise PreviewError("path must be relative to <workspace>/Project, got an absolute path")
    if any(part == ".." for part in parts):
        raise PreviewError("path traversal ('..') is rejected")
    if len(parts) < 2 or parts[0].lower() != PREVIEW_SUBDIR:
        raise PreviewError(
            "preview files must live in %s/ inside <workspace>/Project, e.g. '%s/model.png'"
            % (PREVIEW_SUBDIR, PREVIEW_SUBDIR)
        )
    relative = "/".join(parts)
    # Reuses the project-wide validation (absolute paths, '..', symlinks, junctions).
    target = config.resolve_project_path(relative)
    root = _previews_root()
    if target != root and root not in target.parents:
        raise PreviewError("resolved preview path escapes %s" % root)
    return relative, target


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_capture_preview",
        description=(
            "Render the current model and return the IMAGE ITSELF as MCP image content (base64 PNG the "
            "caller can look at), plus JSON metadata: the real saved path under "
            "<workspace>/Project/previews, the mode that was actually used, the pixel size, byte size and "
            "diagnostics. mode='auto' picks the real 3D viewport when a GUI Blender is running and otherwise "
            "renders through a temporary camera framed on the EVALUATED model bounds (so Geometry Nodes "
            "output is what you see); mode='viewport' insists on the GUI viewport and fails with an "
            "actionable error in --background; mode='render' always uses a camera render. view presets: "
            "current, isometric, front, top, back, right. Sizes are bounded to <=2048 px and the PNG to "
            "<=3 MiB. Render settings, the scene camera, the frame and all temporary datablocks are restored "
            "even on failure, and the .blend is never saved automatically."
        ),
        structured_output=False,
    )
    def blender_capture_preview(
        object: str | None = None,
        path: str | None = None,
        mode: Literal["auto", "viewport", "render"] = "auto",
        view: Literal["current", "isometric", "front", "top", "back", "right"] = "isometric",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        samples: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        overwrite: bool = False,
        transparent: bool = False,
    ) -> CallToolResult:
        width, height = _validate_dimensions(width, height)
        try:
            if timeout is not None and (timeout <= 0 or timeout > 300):
                raise PreviewError("timeout must be between 0 and 300 seconds")
            relative, target = validate_relative_path(path or default_relative_path())
        except PreviewError as exc:
            raise _tool_error(str(exc)) from exc
        except config.PathValidationError as exc:
            raise _tool_error("invalid preview path: %s" % exc) from exc

        if target.exists() and not overwrite:
            raise _tool_error(
                "preview file already exists: %s (pass overwrite=true to replace it)" % target
            )
        # The directory is created by Blender (the side that owns the file), so
        # this process never needs write access to Project just to validate a path.

        budget = float(timeout or DEFAULT_TIMEOUT)
        params = {
            "object": object,
            "relative_path": relative,
            "mode": mode,
            "view": view,
            "width": width,
            "height": height,
            "samples": samples,
            "timeout_s": budget,
            "overwrite": bool(overwrite),
            "transparent": bool(transparent),
        }
        # The Blender-side render is synchronous, so the client budget must outlast it;
        # ``call`` converts every Blender/bridge failure into a ToolError already.
        result = call(bridge, "capture_preview", params, timeout=budget + 15.0)

        try:
            return _build_result(result, target, budget)
        except PreviewError as exc:
            raise _tool_error(str(exc)) from exc


def _tool_error(message: str) -> ToolError:
    return ToolError(message)


def _build_result(result: dict, target: Path, budget: float) -> CallToolResult:
    encoded = result.get("image_base64")
    if not isinstance(encoded, str) or not encoded:
        raise PreviewError("Blender returned no image data: %s" % json.dumps(
            {k: v for k, v in result.items() if k != "image_base64"}, default=str)[:600])
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PreviewError("Blender returned malformed base64 image data: %s" % exc) from exc
    if not raw.startswith(_PNG_MAGIC):
        raise PreviewError("returned image is not a PNG (magic bytes missing)")
    if len(raw) > MAX_PREVIEW_PNG_BYTES:
        raise PreviewError(
            "rendered PNG is %.2f MiB, above the %.2f MiB cap; lower width/height or samples"
            % (len(raw) / 1048576.0, MAX_PREVIEW_PNG_BYTES / 1048576.0)
        )

    reported = str(result.get("path") or "")
    project = config.project_dir().resolve()
    if reported:
        resolved = Path(reported)
        try:
            resolved = resolved.resolve()
        except OSError as exc:
            raise PreviewError("Blender reported an unusable path %r: %s" % (reported, exc)) from exc
        if resolved != target.resolve() or (project not in resolved.parents and resolved != project):
            raise PreviewError(
                "Blender reported %s, which is not the requested file %s inside %s"
                % (resolved, target, project)
            )
    if not target.exists():
        raise PreviewError("the preview file %s was not written" % target)
    actual_bytes = target.stat().st_size
    if actual_bytes != len(raw):
        raise PreviewError(
            "the preview file on disk is %d bytes but Blender returned %d bytes" % (actual_bytes, len(raw))
        )
    size = result.get("size") or {}
    for name, value in (("width", result.get("width")), ("height", result.get("height"))):
        if isinstance(value, int) and (value < MIN_PREVIEW_DIMENSION or value > MAX_PREVIEW_DIMENSION):
            raise PreviewError("Blender reported %s=%s, outside the allowed range" % (name, value))
    if isinstance(size.get("width"), int) and isinstance(size.get("height"), int):
        if size["width"] < MIN_PREVIEW_DIMENSION or size["width"] > MAX_PREVIEW_DIMENSION:
            raise PreviewError("image header width %s is out of range" % size["width"])
        if size["height"] < MIN_PREVIEW_DIMENSION or size["height"] > MAX_PREVIEW_DIMENSION:
            raise PreviewError("image header height %s is out of range" % size["height"])

    metadata = {k: v for k, v in result.items() if k != "image_base64"}
    metadata.update(
        {
            "path": str(target),
            "relative_path": _relative_to_project(target),
            "bytes": actual_bytes,
            "sha256_prefix": _sha256_prefix(raw),
            "mcp_image_content": True,
            "budget_s": budget,
            "limits": {
                "max_dimension": MAX_PREVIEW_DIMENSION,
                "max_png_bytes": MAX_PREVIEW_PNG_BYTES,
            },
            "note": (
                "the image is attached to this tool result as MCP image content (image/png); the file "
                "above is the same picture saved under <workspace>/Project/previews"
            ),
        }
    )
    text = json.dumps(metadata, ensure_ascii=False, default=str, indent=2)
    return CallToolResult(
        content=[
            ImageContent(type="image", data=encoded, mimeType="image/png"),
            TextContent(type="text", text=text),
        ],
        isError=False,
    )


def _relative_to_project(target: Path) -> str:
    try:
        return target.resolve().relative_to(config.project_dir().resolve()).as_posix()
    except ValueError:  # pragma: no cover - validated above
        return target.as_posix()


def _sha256_prefix(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:32]
