"""Geometry Nodes editor capture: real editor pixels + machine-readable layout.

One MCP tool:

``blender_geometry_nodes_capture_editor``
    Points a real Geometry Nodes editor at exactly one node group, optionally fits
    a node subset, screenshots the **editor area** with
    ``bpy.ops.screen.screenshot_area`` and returns the pixels as native MCP image
    content together with the full canvas structure (nodes, coordinates, parent
    Frames, links with endpoints and multi-input order, Reroutes, interface).

Guarantees
----------
* The request is verified **at draw time**: a draw-handler probe records the tree
  the editor actually painted.  If the editor shows a Shader tree, another group
  or an empty view, the call fails (``target_not_drawn``) instead of returning a
  wrong picture; if no frame is drawn at all (minimised/covered window) it fails
  with ``stale_frame``.
* A missing Geometry Nodes editor (or an explicitly named area of another type)
  is reconfigured temporarily and restored - area type, node tree, pin, path,
  sidebar, view, selection - and the restore is attempted and reported even when
  the capture fails.
* The image is never cropped or synthesised: it is the whole editor area.  A
  ``nodes`` subset changes only the view fit and the returned bounds/coordinates.
* No layout data is modified (node coordinates and links are read-only here), no
  mouse/keyboard input is synthesised and the ``.blend`` is never saved.
"""

from __future__ import annotations

import time
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field

from ..bridge import BridgeClient
from . import review_image
from ._common import call

PREPARE = "gn_editor_capture_prepare"
STATUS = "gn_editor_capture_status"
COMMIT = "gn_editor_capture_commit"
ABORT = "gn_editor_capture_abort"
LAYOUT = "gn_editor_layout"

CAPTURE_TIMEOUT = 60.0
DRAW_WAIT_SECONDS = review_image.DEFAULT_DRAW_WAIT_SECONDS

NodeName = Annotated[str, Field(min_length=1, max_length=255)]
NodeNames = Annotated[list[NodeName], Field(min_length=1, max_length=512)]
Identifier = Annotated[str, Field(min_length=1, max_length=255)]


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_geometry_nodes_capture_editor",
        description=(
            "Screenshot the REAL Geometry Nodes editor area showing an exact node group and return it as "
            "MCP image content (image/png from the native window framebuffer), together with the full "
            "machine-readable node layout (node coordinates, parent Frames, links with endpoints and "
            "multi-input order, Reroutes, interface) read after the confirmed draw. fit='KEEP' (default) "
            "changes no view state; fit='ALL' frames the whole tree; fit='SELECTED' frames 'nodes' (or the "
            "editors' current selection) and reports hidden/collapsed members it cannot frame. "
            "include_sidebar controls the editor's N-panel. window_index/area_index pin the exact window "
            "and area; if the requested area is not a Geometry Nodes editor it is reconfigured temporarily "
            "and restored - area type, node tree/pin/path, sidebar, view, selection and window scene - also "
            "when the capture fails. The image is the whole editor area (no cropping): 'nodes' only affects "
            "the fit and the reported bounds. The request is verified at draw time, so a stale frame, a "
            "Shader tree or an empty editor is a structured error, never a wrong image. The PNG is "
            "published atomically under <workspace>/Project/previews/*.png."
        ),
        structured_output=False,
    )
    def blender_geometry_nodes_capture_editor(
        group: Identifier,
        object: str | None = None,
        modifier: str | None = None,
        window_index: int | None = None,
        area_index: int | None = None,
        fit: Literal["KEEP", "ALL", "SELECTED"] = "KEEP",
        nodes: NodeNames | None = None,
        include_sidebar: bool = True,
        relative_path: str | None = None,
        overwrite: bool = False,
    ) -> CallToolResult:
        started = time.time()
        try:
            relative, target = review_image.validate_relative_path(
                relative_path or review_image.default_relative_path("gn-editor", group)
            )
        except review_image.ReviewImageError as exc:
            raise ToolError(str(exc)) from exc
        if target.exists() and not overwrite:
            raise ToolError(
                "capture file already exists: %s (pass overwrite=true to replace it)" % target
            )
        params = {
            "group": group,
            "object": object,
            "modifier": modifier,
            "window_index": window_index,
            "area_index": area_index,
            "fit": fit,
            "nodes": nodes,
            "include_sidebar": bool(include_sidebar),
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
            metadata["tool"] = "blender_geometry_nodes_capture_editor"
            metadata["budget_s"] = round(time.time() - started, 3)
            return review_image.image_call_result(metadata, started=started)
        except review_image.ReviewImageError as exc:
            raise ToolError(str(exc)) from exc
