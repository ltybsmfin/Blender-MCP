"""Public MCP image plumbing shared by the capture tools (no ``bpy`` import).

This module is the only place that turns a Blender capture answer into **native
MCP image content**.  ``image_result(metadata)`` is intentionally public: a
parent workflow (or another tool module) can reuse it without touching Blender
or importing ``bpy``.

What is enforced here
---------------------
* ``relative_path`` must name a ``previews/<name>.png`` file inside
  ``<workspace>/Project`` (the same rules the add-on applies again on its side).
* The base64 payload must decode, must be a real PNG, must be within the byte and
  dimension limits, and must match the file on disk (size, SHA-256, mtime).
* A file that predates the request (``mtime`` older than the request start) is
  rejected as a stale capture instead of being returned as fresh evidence.
* The text block repeats the machine-readable metadata; the image block carries
  the pixels.  A path alone is never considered "the AI saw the picture".

Nothing here renders, crops or synthesises an image: the pixels always come from
Blender's native window framebuffer API (or screenshot_area fallback).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import time
import uuid
from pathlib import Path

from mcp.types import CallToolResult, ImageContent, TextContent

from .. import config
from ._common import call

#: Where captures may live, relative to ``<workspace>/Project``.
PREVIEW_SUBDIR = "previews"
#: Limits shared with the add-on side (which re-implements them).
MIN_IMAGE_DIMENSION = 8
MAX_IMAGE_DIMENSION = 8192
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_FRAME_BUDGET_BYTES = int(7.5 * 1024 * 1024)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: A published file older than the request by more than this is stale evidence.
STALE_TOLERANCE_SECONDS = 5.0
#: Default wait for a confirmed redraw before a deferred capture gives up.
DEFAULT_DRAW_WAIT_SECONDS = 8.0
DEFAULT_POLL_INTERVAL = 0.08


class ReviewImageError(Exception):
    """An image answer that must be reported as an MCP tool error."""


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def previews_root() -> Path:
    return (config.project_dir() / PREVIEW_SUBDIR).resolve()


def validate_relative_path(relative_path) -> tuple[str, Path]:
    """Validate ``previews/<name>.png`` and return ``(relative, absolute)``."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ReviewImageError("relative_path must be a non-empty string ending in .png")
    raw = relative_path.strip().replace("\\", "/")
    if not raw.lower().endswith(".png"):
        raise ReviewImageError("captures are PNG files; %r does not end in .png" % raw)
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ReviewImageError("relative_path must be relative to <workspace>/Project")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ReviewImageError("path traversal ('..') is rejected")
    if len(parts) < 2 or parts[0].lower() != PREVIEW_SUBDIR:
        raise ReviewImageError(
            "captures are published to <workspace>/Project/%s/*.png, got %r" % (PREVIEW_SUBDIR, raw)
        )
    relative = "/".join(parts)
    try:
        target = config.resolve_project_path(relative)
    except config.PathValidationError as exc:
        raise ReviewImageError("invalid capture path: %s" % exc) from exc
    root = previews_root()
    if target != root and root not in target.parents:
        raise ReviewImageError("resolved capture path escapes %s" % root)
    return relative, target


def _safe_fragment(value, limit=48) -> str:
    text = str(value or "").strip()
    keep = []
    for char in text:
        if char.isalnum() or char in ("-", "_", ".") or "\u4e00" <= char <= "\u9fff":
            keep.append(char)
        else:
            keep.append("_")
    return ("".join(keep).replace("..", "_").strip("_.")[:limit]) or "capture"


def default_relative_path(kind, name=None) -> str:
    """A collision-free name inside ``Project/previews`` (mirrors the add-on)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    tail = "-%s" % _safe_fragment(name) if name else ""
    return "%s/%s%s-%s-%s.png" % (PREVIEW_SUBDIR, _safe_fragment(kind, 24), tail, stamp,
                                  uuid.uuid4().hex[:6])


# --------------------------------------------------------------------------- #
# image evidence
# --------------------------------------------------------------------------- #
def decode_png(encoded) -> tuple[bytes, int, int]:
    if not isinstance(encoded, str) or not encoded:
        raise ReviewImageError("Blender returned no image data (image_base64 is missing or empty)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReviewImageError("Blender returned malformed base64 image data: %s" % exc) from exc
    if not raw.startswith(PNG_MAGIC):
        raise ReviewImageError("the returned image is not a PNG (signature missing)")
    if len(raw) < 33:
        raise ReviewImageError("the returned PNG is truncated (%d bytes)" % len(raw))
    if len(raw) > MAX_PNG_BYTES:
        raise ReviewImageError(
            "the captured PNG is %.2f MiB, above the %.2f MiB cap; capture a smaller window/area"
            % (len(raw) / 1048576.0, MAX_PNG_BYTES / 1048576.0)
        )
    width, height = struct.unpack(">II", raw[16:24])
    for label, value in (("width", width), ("height", height)):
        if value < MIN_IMAGE_DIMENSION or value > MAX_IMAGE_DIMENSION:
            raise ReviewImageError(
                "the captured image reports %s=%d pixels, outside the accepted %d..%d range"
                % (label, value, MIN_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION)
            )
    return raw, int(width), int(height)


def verify_published(metadata: dict, raw: bytes, width: int, height: int, started=None) -> dict:
    """Cross-check the answer against the file that is really on disk."""
    reported_path = metadata.get("path")
    if not reported_path:
        raise ReviewImageError("Blender did not report the published capture path")
    try:
        target = Path(str(reported_path)).resolve()
    except OSError as exc:
        raise ReviewImageError("Blender reported an unusable path %r: %s" % (reported_path, exc)) from exc
    project = config.project_dir().resolve()
    root = previews_root()
    if root not in target.parents:
        raise ReviewImageError("Blender reported %s, which is outside %s" % (target, root))
    if project not in target.parents and target != project:  # pragma: no cover - root check above
        raise ReviewImageError("Blender reported %s, which is outside %s" % (target, project))
    if not target.exists():
        raise ReviewImageError("the capture file %s was not written" % target)
    actual = target.read_bytes()
    if actual != raw:
        raise ReviewImageError(
            "the file on disk (%d bytes) differs from the returned image (%d bytes)"
            % (len(actual), len(raw))
        )
    reported_bytes = metadata.get("bytes")
    if isinstance(reported_bytes, int) and reported_bytes != len(actual):
        raise ReviewImageError("Blender reported %d bytes but the file has %d"
                               % (reported_bytes, len(actual)))
    digest = hashlib.sha256(actual).hexdigest()
    reported_hash = metadata.get("sha256")
    if isinstance(reported_hash, str) and reported_hash and reported_hash != digest:
        raise ReviewImageError("the capture file changed after it was published (sha256 mismatch)")
    for key, value in (("width", width), ("height", height)):
        reported = metadata.get(key)
        if isinstance(reported, int) and reported != value:
            raise ReviewImageError("Blender reported %s=%d but the PNG header says %d"
                                   % (key, reported, value))
    stat_result = target.stat()
    mtime = float(stat_result.st_mtime)
    reported_mtime = metadata.get("mtime")
    if isinstance(reported_mtime, (int, float)) and abs(float(reported_mtime) - mtime) > 1.0:
        raise ReviewImageError("the capture file mtime does not match the reported one")
    if started is not None:
        if mtime < float(started) - STALE_TOLERANCE_SECONDS:
            raise ReviewImageError(
                "stale capture rejected: %s was written %.1fs before this request started"
                % (target, float(started) - mtime)
            )
    return {
        "path": str(target),
        "relative_path": metadata.get("relative_path") or _relative(target),
        "bytes": len(actual),
        "width": width,
        "height": height,
        "sha256": digest,
        "sha256_prefix": digest[:32],
        "mtime": mtime,
        "mtime_ns": int(stat_result.st_mtime_ns),
        "verified_seconds_before_now": round(time.time() - mtime, 3),
    }


def _relative(target: Path) -> str:
    try:
        return target.resolve().relative_to(config.project_dir().resolve()).as_posix()
    except ValueError:  # pragma: no cover - validated above
        return target.as_posix()


# --------------------------------------------------------------------------- #
# public helpers
# --------------------------------------------------------------------------- #
def image_result(metadata: dict, image_base64=None, *, started=None):
    """``[TextContent, ImageContent]`` for one capture answer.

    ``metadata`` is the JSON object a capture command returned; the base64 image
    may live in ``metadata["image_base64"]`` (Blender's own field) or be passed
    explicitly.  The returned list is ready to use as an MCP tool result.
    """
    if not isinstance(metadata, dict):
        raise ReviewImageError("capture metadata must be a JSON object")
    fields = dict(metadata)
    encoded = image_base64 if image_base64 is not None else fields.pop("image_base64", None)
    raw, width, height = decode_png(encoded)
    evidence = verify_published(fields, raw, width, height, started=started)
    fields.update({
        "path": evidence["path"],
        "relative_path": evidence["relative_path"],
        "bytes": evidence["bytes"],
        "width": evidence["width"],
        "height": evidence["height"],
        "size": {"width": evidence["width"], "height": evidence["height"]},
        "sha256": evidence["sha256"],
        "sha256_prefix": evidence["sha256_prefix"],
        "mtime": evidence["mtime"],
        "mtime_ns": evidence["mtime_ns"],
        "mcp_image_content": True,
        "image_mime_type": "image/png",
        "limits": {
            "max_png_bytes": MAX_PNG_BYTES,
            "min_dimension": MIN_IMAGE_DIMENSION,
            "max_dimension": MAX_IMAGE_DIMENSION,
            "frame_budget_bytes": MAX_FRAME_BUDGET_BYTES,
        },
        "note": (
            "the pixels are attached to this tool result as MCP image content (image/png) and were "
            "captured from the real editor framebuffer using %s; the path above is the same picture "
            "saved under <workspace>/Project/previews" % fields.get("image_source", "the native capture API")
        ),
    })
    text = json.dumps(fields, ensure_ascii=False, default=str, indent=2)
    return [
        TextContent(type="text", text=text),
        ImageContent(type="image", data=encoded, mimeType="image/png"),
    ]


def image_call_result(metadata: dict, image_base64=None, *, started=None) -> CallToolResult:
    """``CallToolResult`` wrapper around :func:`image_result`."""
    content = image_result(metadata, image_base64, started=started)
    return CallToolResult(content=content, isError=False)


def guard_frame_budget(metadata: dict, raw: bytes) -> None:
    """Refuse an answer that could not have fitted the bridge frame."""
    encoded_len = 4 * (len(raw) + 2) // 3
    meta_len = len(json.dumps({key: value for key, value in metadata.items() if key != "image_base64"},
                              default=str))
    total = encoded_len + meta_len
    if total > MAX_FRAME_BUDGET_BYTES:  # pragma: no cover - the add-on refuses first
        raise ReviewImageError(
            "the capture plus metadata would need about %.2f MiB, above the %.2f MiB bridge frame budget"
            % (total / 1048576.0, MAX_FRAME_BUDGET_BYTES / 1048576.0)
        )


# --------------------------------------------------------------------------- #
# deferred capture orchestration (prepare -> poll status -> commit -> abort)
# --------------------------------------------------------------------------- #
def run_deferred_capture(bridge, *, prepare_command, status_command, commit_command, abort_command,
                         prepare_params: dict, relative_path: str, overwrite: bool,
                         wait_seconds: float = DEFAULT_DRAW_WAIT_SECONDS,
                         poll_interval: float = DEFAULT_POLL_INTERVAL,
                         timeout: float = 30.0) -> tuple[dict, dict]:
    """Drive one prepare/poll/commit/abort capture session.

    The capture is deliberately deferred: the add-on's bridge handler is
    synchronous and cannot pump Blender's UI event loop, so the redraw that makes
    ``screen.screenshot_area`` meaningful happens in the main loop *between* the
    bridge calls.  The status poll waits for the draw probe to confirm that the
    requested editor state was really drawn; without that confirmation a commit is
    refused (``stale_frame``) instead of returning an old picture.
    """
    started = time.time()
    prepared = call(bridge, prepare_command, dict(prepare_params), timeout=timeout)
    session = prepared.get("session")
    if not isinstance(session, str) or not session:
        raise ReviewImageError("Blender armed no capture session: %s"
                               % json.dumps(prepared, default=str)[:400])
    deadline = time.monotonic() + max(float(wait_seconds), 0.1)
    status = None
    waited = 0.0
    try:
        while True:
            status = call(bridge, status_command, {"session": session}, timeout=timeout)
            if status.get("committed"):
                raise ReviewImageError("the capture session %s was already committed" % session)
            match = status.get("match") or {}
            if int(status.get("draws_after_arm") or 0) > 0 and match.get("matches"):
                break
            if status.get("expired"):
                raise ReviewImageError(
                    "the capture session expired before Blender drew the requested state "
                    "(no screenshot was returned)"
                )
            if time.monotonic() >= deadline:
                raise ReviewImageError(
                    "no redraw of the requested editor was observed within %.1fs, so no screenshot was "
                    "taken (the Blender window is probably minimised or covered). probe=%s"
                    % (wait_seconds, json.dumps(status.get("probe"), default=str)[:400])
                )
            time.sleep(poll_interval)
            waited = time.time() - started
        result = call(bridge, commit_command, {
            "session": session,
            "relative_path": relative_path,
            "overwrite": bool(overwrite),
        }, timeout=timeout)
        orchestration = {
            "session": session,
            "prepare": prepared,
            "status": status,
            "started_at": started,
            "waited_s": round(waited, 3),
            "total_s": round(time.time() - started, 3),
            "protocol": "prepare -> poll status until a draw showing the target was observed -> commit "
                        "-> abort (idempotent release)",
        }
        return result, orchestration
    finally:
        # A committed session is already closed; abort then reports "nothing to
        # restore".  Any other exit path (timeout, bridge error) must not leave a
        # draw handler or a temporarily reconfigured editor behind.
        try:
            call(bridge, abort_command, {"session": session}, timeout=timeout)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
            pass
