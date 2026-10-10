"""Model inspection tools: evaluated geometry facts and a real viewport screenshot.

Two MCP tools live here (both forward to named bridge commands, no arbitrary
Python and no ``bpy`` import in this process):

``blender_model_inspection_inspect``
    Read-only summary of what one object's modifier stack really evaluates to in
    an exactly named scene: component types, counts, world-space bounds, a
    comparable signature, the modifier stack and honest ``warnings``/``unknowns``.
    The add-on takes the depsgraph from the **target scene** (never from
    ``bpy.context``), releases every temporary mesh in a ``finally`` block and
    never applies a modifier, changes a transform or saves the ``.blend``.

``blender_model_inspection_capture_viewport``
    The real pixels of the 3D viewport area (``bpy.ops.screen.screenshot_area``,
    returned as native MCP image content) plus the evaluated summary of the same
    object at capture time.  There is no camera render / OpenGL still-render /
    cached-file fallback, and no mouse or focus automation.
"""

from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from ..bridge import BridgeClient
from . import review_image
from ._common import call

#: Bridge commands this domain owns.
INSPECT = "model_inspect"
PREPARE = "viewport_capture_prepare"
STATUS = "viewport_capture_status"
COMMIT = "viewport_capture_commit"
ABORT = "viewport_capture_abort"

#: The bridge handler is synchronous; these budgets bound the round trips.
INSPECT_TIMEOUT = 90.0
CAPTURE_TIMEOUT = 60.0
DRAW_WAIT_SECONDS = review_image.DEFAULT_DRAW_WAIT_SECONDS


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_model_inspection_inspect",
        description=(
            "Read-only evaluated-model inspection of ONE object in ONE exact scene. Evaluates the full "
            "modifier stack through that scene's own dependency graph (never the active scene's) and "
            "reports: evaluated geometry component types and counts (mesh vertices/edges/polygons/loops, "
            "curve splines/control points and their tessellation, point clouds), world-space bounds with "
            "their exact source, instance count/bounds, a comparable signature, the modifier stack, and "
            "explicit 'warnings'/'unknowns' for anything the Blender API could not prove. Nothing is "
            "applied or modified: the mesh data, modifier stack and transform are untouched, every "
            "temporary mesh is released with to_mesh_clear(), and the .blend is never saved. 'modifier' "
            "only selects which modifier's details are reported - a partial stack evaluation is not "
            "offered because Blender can only produce it by modifying the object."
        ),
    )
    def blender_model_inspection_inspect(
        object: str,
        scene: str | None = None,
        modifier: str | None = None,
    ) -> dict:
        return call(
            bridge,
            INSPECT,
            {"object": object, "scene": scene, "modifier": modifier},
            timeout=INSPECT_TIMEOUT,
        )

    @mcp.tool(
        name="blender_model_inspection_capture_viewport",
        description=(
            "Capture the REAL 3D viewport area of a running GUI Blender as MCP image content "
            "(image/png via the native window framebuffer) together with the evaluated summary of the "
            "captured object, the target scene, view/fit and size information. No camera render, no "
            "OpenGL still-render and no cached-file fallback: the capture is committed only after a real "
            "draw of the requested scene was observed, otherwise the call fails instead of returning a "
            "stale picture. window_index/area_index select the exact window and area; 'fit' frames the "
            "object (selection, active object, window scene and view are restored afterwards, also on "
            "failure). The PNG is published atomically under <workspace>/Project/previews/*.png "
            "(relative_path must be 'previews/<name>.png'; overwrite=true replaces an existing file). "
            "No mouse automation, no focus stealing, no .blend save."
        ),
        structured_output=False,
    )
    def blender_model_inspection_capture_viewport(
        object: str,
        scene: str | None = None,
        window_index: int | None = None,
        area_index: int | None = None,
        fit: bool = True,
        relative_path: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        started = time.time()
        try:
            relative, target = review_image.validate_relative_path(
                relative_path or review_image.default_relative_path("viewport", object)
            )
        except review_image.ReviewImageError as exc:
            raise ToolError(str(exc)) from exc
        if target.exists() and not overwrite:
            raise ToolError(
                "capture file already exists: %s (pass overwrite=true to replace it)" % target
            )
        params = {
            "object": object,
            "scene": scene,
            "window_index": window_index,
            "area_index": area_index,
            "fit": bool(fit),
        }
        try:
            result, orchestration = review_image.run_deferred_capture(
                bridge,
                prepare_command=PREPARE,
                status_command=STATUS,
                commit_command=COMMIT,
                abort_command=ABORT,
                prepare_params=params,
                relative_path=relative,
                overwrite=bool(overwrite),
                wait_seconds=DRAW_WAIT_SECONDS,
                timeout=CAPTURE_TIMEOUT,
            )
            metadata = dict(result)
            metadata["capture_session"] = orchestration
            metadata["tool"] = "blender_model_inspection_capture_viewport"
            metadata["budget_s"] = round(time.time() - started, 3)
            return review_image.image_call_result(metadata, started=started)
        except review_image.ReviewImageError as exc:
            raise ToolError(str(exc)) from exc
