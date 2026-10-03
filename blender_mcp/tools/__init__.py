"""Tool registration and the FastMCP application factory."""

from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import audit
from ..bridge import BridgeClient
from . import drivers, geometry_nodes, launcher, meta, operation_log, preview, scene, shape_keys

INSTRUCTIONS = """\
Prefer editable Geometry Nodes for parametric modeling; inspect real sockets before linking,
expose useful group inputs, and verify evaluated geometry after changing parameters. Support
shape keys on original mesh topology and drivers with explicit data paths. Call blender_health
first; if unavailable, call blender_launch_blender. Keep node groups and modifiers editable.
Blender MCP server (stdio). It drives a **running** Blender through a local, token-authenticated
JSON bridge on 127.0.0.1 (default port 9876). Nothing here executes arbitrary Python in Blender:
every capability is a typed tool with explicit validation, and driver expressions are the only
expression channel (they are Blender driver expressions, not Python eval).

Workflow contract
-----------------
1. Call `blender_health` first. It returns the exact Blender version, the loaded .blend, the scene
   and the command list. If it fails, no bridge is running: start Blender with `blender_launch_blender`
   (or run `MCP/blender/bootstrap.py` inside an open Blender) before retrying.
2. Prefer **procedural geometry nodes** over destructive edits. Keep existing modifiers intact;
   adding a Geometry Nodes modifier is additive, and parameters stay editable.
3. **Inspect before you link.** Node socket display names are localized and version dependent;
   identifiers are the stable handle. Use `blender_inspect_node_group` on existing graphs and
   `blender_describe_node_type` / `blender_list_node_types` for runtime discovery of what this
   Blender build actually provides (do not assume a node or socket name exists).
4. Build graphs with `blender_batch_graph_edit` when you have more than one edit. It is
   **transactional by default**: every operation is applied to a staging copy of the node group and
   the group is only replaced when all of them succeeded, so a failure leaves the original group -
   nodes, links, defaults, interface, modifier inputs, nested group references and drivers - exactly
   as it was. Read `committed` / `rolled_back` / `failures` and do not assume anything else.
   `continue_on_error=true` is only valid together with `transactional=false`, which restores the
   old non-atomic behaviour (partial edits stay applied) and must then be repaired explicitly.
5. Verify with data, not with hope: `blender_object_info`, and the `evaluated` /
   `signature` fields returned by modifier and shape key tools, report the evaluated mesh counts and
   world-space bounds produced by the dependency graph. `blender_capture_preview` returns the actual
   rendered PNG as MCP image content when you need to look at the model.
6. Shape keys require the object's **original mesh topology**: one coordinate per original vertex.
   Basis is created automatically and protected. A Geometry Nodes graph using the original geometry
   can preserve shape-key deformation; geometry generated independently from primitives may ignore
   it. Choose the geometry flow accordingly and verify the result.
7. Drivers evaluate on frame changes and dependency-graph updates. After changing a driver's inputs
   call `blender_set_frame` (to a different frame) or `blender_update_depsgraph`, then read the value.
   Owners supported: object, shape_keys, node_group, scene. `data_path` values are resolved with
   Blender's `ID.path_resolve`, never evaluated as code.
8. Saving is restricted to `<workspace>/Project`. Absolute paths, `..` escapes and symlink/junction
   components are rejected. `blender_save_blend` refuses to overwrite unless `overwrite=true`.
   Previews are written only to `<workspace>/Project/previews/*.png`.
9. Paths, topology, socket compatibility and driver data paths are validated before mutation; a
   failure is reported as a tool error (isError) with a machine-readable code, never as a success
   payload containing "error".
10. Every operation is audited: arguments (secrets removed, large arrays hashed), outcome, error and
   duration are appended to `<MCP>/.runtime/logs/audit-*.jsonl`. Read them with
   `blender_operation_log`, or call `blender_log_dashboard` to get a live local URL for the
   read-only dashboard (Blender side events are linked by the same request id).
"""

#: How many batch steps are mirrored into the audit log as individual events.
MAX_AUDITED_STEPS = 40


class AuditedFastMCP(FastMCP):
    """FastMCP with an audit record around every tool call.

    ``FastMCP._setup_handlers`` binds ``self.call_tool``, so overriding it here
    captures *every* call - including the ones that fail argument validation
    inside the SDK, before a single line of tool code runs. That is the only place
    where a schema rejection can still be observed.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        log = audit.get_audit()
        request_id = audit.new_request_id()
        started = time.perf_counter()
        try:
            safe_args = log.sanitizer.fields(arguments or {})
        except Exception:  # noqa: BLE001 - logging must never break the call
            safe_args = {"_sanitize_error": True}
        with audit.request_scope(request_id, tool=name):
            log.record("tool_start", request_id=request_id, tool=name, status="running", args=safe_args)
            try:
                result = await super().call_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001 - re-raised unchanged
                duration = round((time.perf_counter() - started) * 1000.0, 3)
                extra = _batch_audit(log, request_id, name, _error_payload(exc))
                log.record(
                    "tool_end",
                    request_id=request_id,
                    tool=name,
                    status=extra.pop("status", "error"),
                    ok=False,
                    duration_ms=duration,
                    error_type=type(exc).__name__,
                    code=getattr(exc, "code", None),
                    message=str(exc),
                    **extra,
                )
                raise
            duration = round((time.perf_counter() - started) * 1000.0, 3)
            extra = _batch_audit(log, request_id, name, audit.result_mapping(result))
            try:
                summary = audit.summarize_result(result, log.sanitizer)
            except Exception:  # noqa: BLE001
                summary = {"_type": type(result).__name__}
            status = extra.pop("status", "ok")
            log.record(
                "tool_end",
                request_id=request_id,
                tool=name,
                status=status,
                ok=status == "ok",
                duration_ms=duration,
                result=summary,
                **extra,
            )
            return result


def _error_payload(exc: BaseException) -> dict | None:
    """A tool's ``audit_payload``, found through the SDK's ToolError wrapping.

    ``Tool.run`` re-raises a fresh ``ToolError`` with the original as ``__cause__``,
    so the payload attached by the tool has to be looked up along that chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        payload = getattr(current, "audit_payload", None)
        if isinstance(payload, dict):
            return payload
        current = current.__cause__ or current.__context__
    return None


def _batch_audit(log: audit.AuditLog, request_id: str, name: str, result) -> dict:
    """Mirror a batched graph edit's steps and verdict into the audit log."""
    if not isinstance(result, dict):
        return {}
    transaction = result.get("transaction")
    if not isinstance(transaction, dict):
        return {}
    steps = []
    for entry in (result.get("completed") or [])[:MAX_AUDITED_STEPS]:
        if isinstance(entry, dict):
            steps.append({"index": entry.get("index"), "op": entry.get("op"), "ok": True})
    for entry in (result.get("failures") or [])[:MAX_AUDITED_STEPS]:
        if isinstance(entry, dict):
            steps.append({
                "index": entry.get("index"),
                "op": entry.get("op"),
                "ok": False,
                "code": entry.get("code"),
                "error": entry.get("error"),
            })
    for step in steps:
        log.record(
            "batch_step",
            request_id=request_id,
            tool=name,
            group=result.get("group"),
            status="rolled_back" if not step["ok"] and transaction.get("status") == "rolled_back" else (
                "ok" if step["ok"] else "error"),
            **step,
        )
    status = "ok"
    if result.get("rolled_back"):
        status = "rolled_back"
        log.record(
            "batch_rollback",
            request_id=request_id,
            tool=name,
            status="rolled_back",
            group=result.get("group"),
            reason=transaction.get("status"),
            failures=result.get("failures"),
            details=transaction,
        )
    elif result.get("failed_count"):
        status = "error"
    return {
        "status": status,
        "committed": result.get("committed"),
        "rolled_back": result.get("rolled_back"),
        "transaction": transaction,
        "steps": steps,
    }


def build_server(bridge: BridgeClient | None = None, name: str = "blender") -> FastMCP:
    """Create the FastMCP app with every tool registered."""
    client = audit.instrument_bridge(bridge or BridgeClient())
    mcp = AuditedFastMCP(name=name, instructions=INSTRUCTIONS)
    meta.register(mcp, client)
    scene.register(mcp, client)
    geometry_nodes.register(mcp, client)
    shape_keys.register(mcp, client)
    drivers.register(mcp, client)
    launcher.register(mcp, client)
    preview.register(mcp, client)
    operation_log.register(mcp, client)
    return mcp
