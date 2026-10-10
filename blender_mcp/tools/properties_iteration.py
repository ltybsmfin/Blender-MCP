"""One bounded parameter change followed by evaluated data and real UI pixels."""
from __future__ import annotations

import json
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from ._common import call
from .properties import Name, Changes
from . import review_image


def register(mcp, bridge):
    @mcp.tool(name="blender_properties_iterate_parameters", structured_output=False,
              annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                          idempotentHint=False, openWorldHint=False),
              meta={"domain": "properties"},
              description="Change up to 128 exact modifier instance inputs atomically, then return parameter diff, before/after evaluated model inspection and a REAL viewport screenshot as MCP image. Capture failure after a successful write is an error with parameters_committed=true; it does not pretend the write was rolled back. No scene save or shared defaults change.")
    def blender_properties_iterate_parameters(
        object: Name, changes: Changes, modifier: Name | None = None,
        scene: Name | None = None, window_index: int | None = None,
        area_index: int | None = None, fit: bool = True,
        relative_path: str | None = None, overwrite: bool = False,
    ) -> CallToolResult:
        import time
        started = time.time()
        try:
            relative, target = review_image.validate_relative_path(
                relative_path or review_image.default_relative_path("iteration", object))
        except review_image.ReviewImageError as exc:
            raise ToolError(str(exc)) from exc
        if target.exists() and not overwrite:
            raise ToolError("capture file already exists: %s" % target)
        params = {"object": object, "modifier": modifier, "scene": scene}
        before = call(bridge, "model_inspect", params)
        written = call(bridge, "properties_set_parameters", {
            "object": object, "modifier": modifier,
            "changes": [c.model_dump() for c in changes]})
        report = {"object": object, "modifier": modifier, "parameters_committed": True,
                  "parameter_diff": written, "before": before}
        try:
            report["after"] = call(bridge, "model_inspect", params)
            capture, orchestration = review_image.run_deferred_capture(
                bridge, prepare_command="viewport_capture_prepare",
                status_command="viewport_capture_status", commit_command="viewport_capture_commit",
                abort_command="viewport_capture_abort",
                prepare_params={"object": object, "scene": scene, "window_index": window_index,
                                "area_index": area_index, "fit": fit},
                relative_path=relative, overwrite=overwrite,
                wait_seconds=review_image.DEFAULT_DRAW_WAIT_SECONDS, timeout=60.0)
            capture["capture_session"] = orchestration
            content = review_image.image_result(capture, started=started)
        except (ToolError, review_image.ReviewImageError) as exc:
            report.update(code="iteration_inspection_failed", message=str(exc))
            raise ToolError(json.dumps(report, ensure_ascii=False)) from exc
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(report, ensure_ascii=False)),
                                       *content], isError=False)
