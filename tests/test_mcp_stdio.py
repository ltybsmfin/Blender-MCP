"""End-to-end test of the MCP stdio server with a real Blender bridge.

What this proves
----------------
1. A real MCP stdio client (official SDK 1.x) can ``initialize`` the server,
   ``list_tools`` and ``call_tool``.
2. Every geometry-nodes / shape-key / driver tool works against a real Blender
   process, in this case a ``--background --factory-startup`` Blender whose
   socket loop is pumped by ``bootstrap.py --mcp-pump`` (Blender never fires
   ``bpy.app.timers`` headless, so the pump is the documented harness path).
3. Invalid input - unsafe save paths, an existing file without overwrite, an
   unknown socket, an incompatible link, a shape-key topology mismatch, a bad
   driver path/expression, an unknown object - comes back as an MCP **error**
   (``isError``), never as a success payload.
4. A wrong bridge token and a dead bridge port are errors too (auth + transport).
5. Only the processes this test started are torn down, with bounded waits.

Run::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_mcp_stdio.py

Report: ``MCP/tests/reports/stdio_report.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession

from stdio_pipe_transport import last_teardown, mcp_stdio_session

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
WORKSPACE = MCP_DIR.parent
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(TESTS_DIR))

from blender_mcp import config  # noqa: E402  (after sys.path setup)

REPORT_DIR = TESTS_DIR / "reports"
REPORT_PATH = REPORT_DIR / "stdio_report.json"
PYTHON = MCP_DIR / ".venv" / "Scripts" / "python.exe"
SERVER_PY = MCP_DIR / "server.py"
BOOTSTRAP = MCP_DIR / "blender" / "bootstrap.py"
BLENDER = Path(os.environ.get("BLENDER_MCP_BLENDER") or "")

TEST_PORT = int(os.environ.get("BLENDER_MCP_TEST_PORT", "0"))
SAVE_RELATIVE = "demo/mcp_stdio_probe.blend"
CALL_TIMEOUT = 120.0
BLENDER_START_TIMEOUT = 120.0
TEARDOWN_TIMEOUT = 20.0

RESULTS: list[dict] = []


def record(name: str, ok: bool, details=None) -> bool:
    entry = {"name": name, "ok": bool(ok)}
    if details is not None:
        entry["details"] = details
    RESULTS.append(entry)
    print("STDIO::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str)))
    return bool(ok)


def find_blender() -> Path:
    env = os.environ.get("BLENDER_MCP_BLENDER")
    if env and Path(env).is_file():
        return Path(env)
    version_dir = WORKSPACE / "Version"
    if version_dir.is_dir():
        for child in sorted(version_dir.iterdir(), reverse=True):
            candidate = child / "blender.exe"
            if candidate.is_file():
                return candidate
    raise RuntimeError("no Blender executable found (set BLENDER_MCP_BLENDER)")


def payload(call_result) -> dict:
    """Normalise an MCP tool result into a dict."""
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        # FastMCP wraps non-object results; unwrap a single 'result' key if present.
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for item in getattr(call_result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


def result_text(call_result) -> str:
    parts = []
    structured = getattr(call_result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, default=str))
    for item in getattr(call_result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def env_for_server(**overrides) -> dict:
    env = dict(os.environ)
    env["BLENDER_MCP_PORT"] = str(TEST_PORT)
    env["BLENDER_MCP_TIMEOUT"] = "60"
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def open_session(**overrides):
    """Open a real MCP stdio client session against ``server.py``.

    Uses the official ``mcp.ClientSession``; the pipe plumbing is the stdlib
    variant from ``stdio_pipe_transport`` because the sandbox denies asyncio's
    Proactor pipe creation (see that module's docstring).
    """
    return mcp_stdio_session(
        command=PYTHON,
        args=[str(SERVER_PY)],
        env=env_for_server(**overrides),
        stderr_path=MCP_DIR / ".runtime" / "logs" / "mcp_server_stderr.log",
    )


def wait_for_bridge(port: int, token_path: Path, timeout: float) -> dict | None:
    """Poll the bridge directly (raw protocol) until it answers, bounded."""
    token = token_path.read_text(encoding="utf-8").strip()
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
                sock.settimeout(5.0)
                sock.sendall(
                    (
                        json.dumps({"id": "probe", "token": token, "command": "health", "params": {}}) + "\n"
                    ).encode("utf-8")
                )
                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                if b"\n" in buffer:
                    response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
                    if response.get("ok"):
                        return response["result"]
                    last = response
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.5)
    return None if last else None


async def call(session: ClientSession, name: str, arguments: dict, timeout: float = CALL_TIMEOUT):
    return await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)


async def expect_error(session: ClientSession, label: str, tool: str, arguments: dict, needle: str | None = None):
    """Assert that a tool call is an error and (optionally) mentions a code."""
    try:
        result = await call(session, tool, arguments)
    except Exception as exc:  # noqa: BLE001 - a raised error is still a failure of the call
        text = "%s: %s" % (type(exc).__name__, exc)
        return record(label, needle is None or needle in text, {"raised": text})
    text = result_text(result)
    if not result.isError:
        return record(label, False, {"unexpected_success": text[:400]})
    return record(label, needle is None or needle in text, {"error_text": text[:400]})


async def expect_ok(session: ClientSession, label: str, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT):
    result = await call(session, tool, arguments, timeout=timeout)
    if result.isError:
        return record(label, False, {"error": result_text(result)[:400]}), {}
    data = payload(result)
    record(label, True, {"keys": sorted(data)[:8]})
    return True, data


async def run_positive(session: ClientSession) -> dict:
    context: dict = {}

    ok, tools = await expect_ok(session, "initialize_and_list_tools", "blender_health", {})
    listing = await session.list_tools()
    names = sorted(tool.name for tool in listing.tools)
    required = {
        "blender_health", "blender_scene_summary", "blender_object_info", "blender_create_mesh_object",
        "blender_create_node_group", "blender_create_node", "blender_set_socket_default",
        "blender_link_sockets", "blender_inspect_node_group", "blender_attach_geometry_nodes_modifier",
        "blender_set_modifier_input", "blender_describe_node_type", "blender_batch_graph_edit",
        "blender_create_shape_key", "blender_set_shape_key_coordinates", "blender_set_shape_key",
        "blender_add_driver", "blender_inspect_drivers", "blender_save_blend", "blender_launch_blender",
        "blender_capture_preview", "blender_operation_log", "blender_log_dashboard",
    }
    record("required_tools_listed", required.issubset(set(names)), {"count": len(names), "missing": sorted(required - set(names))})
    context["tool_count"] = len(names)
    batch_tool = next(tool for tool in listing.tools if tool.name == "blender_batch_graph_edit")
    record(
        "batch_tool_is_transactional_by_default",
        batch_tool.inputSchema["properties"]["transactional"]["default"] is True,
        {"transactional": batch_tool.inputSchema["properties"]["transactional"]},
    )

    ok, health = await expect_ok(session, "health_reports_blender_version", "blender_health", {})
    record(
        "health_reports_real_blender",
        health.get("blender_version", "").startswith("5.") and health.get("server", {}).get("running") is True,
        {"blender_version": health.get("blender_version"), "pid": health.get("pid"),
         "server": health.get("server")},
    )
    context["blender_version"] = health.get("blender_version")

    ok, created = await expect_ok(
        session,
        "create_mesh_object",
        "blender_create_mesh_object",
        {
            "name": "MCP_Panel",
            "vertices": [[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]],
            "faces": [[0, 1, 2, 3]],
        },
    )
    record("create_mesh_object_counts", created.get("vertices") == 4 and created.get("polygons") == 1, created)

    ok, node_types = await expect_ok(
        session, "list_node_types_runtime", "blender_list_node_types", {"prefix": "GeometryNode", "contains": "Mesh", "limit": 200}
    )
    record(
        "list_node_types_finds_mesh_nodes",
        node_types.get("count", 0) > 0 and any("Mesh" in t for t in node_types.get("types", [])),
        {"count": node_types.get("count"), "sample": node_types.get("types", [])[:5]},
    )

    ok, described = await expect_ok(
        session, "describe_node_type_actual_sockets", "blender_describe_node_type", {"node_type": "GeometryNodeMeshGrid"}
    )
    socket_names = {s["name"] for s in described.get("inputs", [])}
    record(
        "describe_node_type_reports_grid_sockets",
        {"Size X", "Vertices X", "Vertices Y"}.issubset(socket_names),
        {"inputs": described.get("inputs")},
    )

    ok, group = await expect_ok(
        session,
        "create_node_group_with_interface",
        "blender_create_node_group",
        {
            "name": "MCP_ParametricGroup",
            "if_exists": "replace",
            "interface": [
                {"name": "Size", "socket_type": "NodeSocketFloat", "in_out": "INPUT", "default_value": 2.0,
                 "min_value": 0.1, "max_value": 20.0, "subtype": "DISTANCE"},
                {"name": "Density", "socket_type": "NodeSocketInt", "in_out": "INPUT", "default_value": 4,
                 "min_value": 1, "max_value": 100},
                {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
            ],
        },
    )
    record("node_group_created", group.get("created") is True, {"group": group.get("group", {}).get("name")})

    ok, batch = await expect_ok(
        session,
        "batch_graph_edit",
        "blender_batch_graph_edit",
        {
            "group": "MCP_ParametricGroup",
            "operations": [
                {"op": "create_node", "node_type": "GeometryNodeMeshGrid", "name": "Grid", "location": [-200, 0]},
                {"op": "create_node", "node_type": "GeometryNodeTransform", "name": "XForm", "location": [200, 0]},
                {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh", "to_node": "XForm", "to_socket": "Geometry"},
                {"op": "link_sockets", "from_node": "XForm", "from_socket": "Geometry",
                 "to_node": "Group Output", "to_socket": "Geometry"},
                {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
                 "to_node": "Grid", "to_socket": "Size X"},
                {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
                 "to_node": "Grid", "to_socket": "Size Y"},
                {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Density",
                 "to_node": "Grid", "to_socket": "Vertices X"},
                {"op": "set_socket_default", "node": "Grid", "socket": "Vertices Y", "value": 4},
            ],
        },
    )
    record(
        "batch_graph_edit_all_operations_completed",
        batch.get("completed_count") == 8 and batch.get("failed_count") == 0 and batch.get("rollback") is False,
        {"completed": batch.get("completed_count"), "failures": batch.get("failures")},
    )
    record(
        "batch_graph_edit_reports_a_committed_transaction",
        batch.get("transactional") is True and batch.get("committed") is True
        and batch.get("rolled_back") is False
        and batch.get("transaction", {}).get("status") == "committed",
        {"transactional": batch.get("transactional"), "transaction": batch.get("transaction")},
    )

    ok, inspected = await expect_ok(
        session, "inspect_node_group", "blender_inspect_node_group", {"name": "MCP_ParametricGroup"}
    )
    record(
        "inspect_node_group_reports_nodes_and_links",
        len(inspected.get("nodes", [])) == 4 and len(inspected.get("links", [])) == 5,
        {"nodes": [n["name"] for n in inspected.get("nodes", [])], "links": len(inspected.get("links", []))},
    )

    ok, attached = await expect_ok(
        session,
        "attach_modifier",
        "blender_attach_geometry_nodes_modifier",
        {"object": "MCP_Panel", "group": "MCP_ParametricGroup", "parameters": {"Size": 2.0, "Density": 4}},
    )
    record(
        "attach_modifier_evaluates_geometry",
        attached.get("evaluated", {}).get("evaluated", {}).get("vertices") == 16,
        {"evaluated": attached.get("evaluated", {}).get("evaluated"),
         "parameters": attached.get("parameters")},
    )

    ok, density = await expect_ok(
        session,
        "set_modifier_input_density",
        "blender_set_modifier_input",
        {"object": "MCP_Panel", "input": "Density", "value": 5},
    )
    vertices = density.get("evaluated", {}).get("evaluated", {}).get("vertices")
    record("modifier_input_change_is_evaluated", vertices == 20, {"vertices": vertices, "expected": 20})

    ok, size = await expect_ok(
        session,
        "set_modifier_input_size",
        "blender_set_modifier_input",
        {"object": "MCP_Panel", "input": "Size", "value": 6.0},
    )
    bounds = size.get("evaluated", {}).get("bounds", {})
    record(
        "modifier_input_bounds_change_is_evaluated",
        bounds.get("size", [0])[0] == 6.0,
        {"bounds": bounds, "expected_size_x": 6.0},
    )

    ok, sphere = await expect_ok(
        session, "create_primitive_sphere", "blender_create_primitive", {"kind": "uv_sphere", "name": "MCP_Sphere", "radius": 1.0}
    )
    vertex_count = sphere.get("vertices", 0)
    ok, key = await expect_ok(
        session,
        "create_shape_key",
        "blender_create_shape_key",
        {"object": "MCP_Sphere", "name": "Wide", "value": 0.0, "slider_min": 0.0, "slider_max": 1.0},
    )
    record(
        "shape_key_creates_basis",
        key.get("basis_created") is True and [k["name"] for k in key.get("keys", [])] == ["Basis", "Wide"],
        {"keys": [k["name"] for k in key.get("keys", [])]},
    )
    deltas = [[1.0, 0.0, 0.0]] * vertex_count
    ok, written = await expect_ok(
        session,
        "set_shape_key_coordinates",
        "blender_set_shape_key_coordinates",
        {"object": "MCP_Sphere", "key_name": "Wide", "deltas": deltas},
    )
    record("shape_key_coordinates_written", written.get("changed") == vertex_count, {"changed": written.get("changed")})

    ok, influence = await expect_ok(
        session, "set_shape_key_influence", "blender_set_shape_key", {"object": "MCP_Sphere", "key_name": "Wide", "value": 1.0}
    )
    max_x = influence.get("evaluated", {}).get("bounds", {}).get("max", [None])[0]
    record(
        "shape_key_influence_changes_evaluated_bounds",
        max_x is not None and abs(max_x - 2.0) < 1e-3,
        {"max_x": max_x, "expected": 2.0, "driven": influence.get("driven")},
    )
    await expect_ok(
        session, "set_shape_key_back", "blender_set_shape_key", {"object": "MCP_Sphere", "key_name": "Wide", "value": 0.0}
    )

    ok, prop = await expect_ok(
        session,
        "set_custom_property",
        "blender_set_custom_property",
        {"owner_kind": "object", "owner_name": "MCP_Sphere", "key": "phase", "value": 0.25},
    )
    record("custom_property_set", prop.get("value") == 0.25, prop)

    ok, driver = await expect_ok(
        session,
        "add_driver",
        "blender_add_driver",
        {
            "owner_kind": "object",
            "owner_name": "MCP_Sphere",
            "data_path": "rotation_euler",
            "index": 2,
            "expression": "phase * 4",
            "variables": [{
                "name": "phase",
                "type": "SINGLE_PROP",
                "targets": [{"id_type": "OBJECT", "id_name": "MCP_Sphere", "data_path": '["phase"]'}],
            }],
        },
    )
    record(
        "driver_added_and_evaluated",
        driver.get("channels", [{}])[0].get("is_valid") is True
        and abs(float(driver.get("evaluated_value", 0.0)) - 1.0) < 1e-6,
        {"evaluated_value": driver.get("evaluated_value"), "expected": 1.0,
         "channels": driver.get("channels")},
    )

    ok, changed = await expect_ok(
        session,
        "custom_property_drives_evaluation",
        "blender_set_custom_property",
        {"owner_kind": "object", "owner_name": "MCP_Sphere", "key": "phase", "value": 0.5},
    )
    ok, info = await expect_ok(session, "object_info_after_driver", "blender_object_info", {"name": "MCP_Sphere"})
    rotation = info.get("rotation_euler", [0, 0, 0])[2]
    record(
        "driver_follows_custom_property",
        abs(rotation - 2.0) < 1e-6,
        {"rotation_euler_z": rotation, "expected": 2.0, "drivers": info.get("drivers")},
    )

    ok, inspected_drivers = await expect_ok(
        session, "inspect_drivers", "blender_inspect_drivers", {"owner_kind": "object", "owner_name": "MCP_Sphere"}
    )
    record(
        "inspect_drivers_reports_target_metadata",
        inspected_drivers.get("drivers", [{}])[0].get("variables", [{}])[0].get("targets", [{}])[0].get("id_name")
        == "MCP_Sphere",
        {"drivers": inspected_drivers.get("drivers")},
    )

    ok, saved = await expect_ok(
        session,
        "save_blend_first_time",
        "blender_save_blend",
        {"relative_path": SAVE_RELATIVE, "overwrite": True},
    )
    record(
        "save_blend_writes_inside_project",
        saved.get("saved") is True and str(saved.get("path", "")).startswith(str(WORKSPACE / "Project")),
        {"path": saved.get("path"), "bytes": saved.get("bytes")},
    )
    context["saved_path"] = saved.get("path")

    ok, scene = await expect_ok(session, "scene_summary", "blender_scene_summary", {"include_objects": True})
    record(
        "scene_summary_lists_objects",
        isinstance(scene.get("objects"), list) and any(o["name"] == "MCP_Panel" for o in scene["objects"]),
        {"objects": [o["name"] for o in scene.get("objects", [])]},
    )

    ok, reuse = await expect_ok(
        session, "launch_blender_reuses_healthy_bridge", "blender_launch_blender", {"reuse_existing": True}
    )
    record(
        "launch_blender_reuses_existing_bridge_without_launching",
        reuse.get("launched") is False and reuse.get("reused") is True,
        {"launched": reuse.get("launched"), "reused": reuse.get("reused"), "reason": reuse.get("reason")},
    )
    return context


async def run_negatives(session: ClientSession) -> None:
    await expect_error(session, "neg_save_path_traversal", "blender_save_blend",
                       {"relative_path": "../../escape.blend", "overwrite": True}, "invalid_path")
    await expect_error(session, "neg_save_absolute_path", "blender_save_blend",
                       {"relative_path": "C:/Windows/evil.blend", "overwrite": True}, "invalid_path")
    await expect_error(session, "neg_save_empty_path", "blender_save_blend",
                       {"relative_path": "", "overwrite": True}, "invalid_path")
    await expect_error(session, "neg_save_without_overwrite", "blender_save_blend",
                       {"relative_path": SAVE_RELATIVE}, "file_exists")

    await expect_error(session, "neg_unknown_object", "blender_object_info", {"name": "NoSuchObject"}, "object_not_found")
    await expect_error(session, "neg_shape_key_unknown_object", "blender_create_shape_key",
                       {"object": "NoSuchObject", "name": "X"}, "object_not_found")
    await expect_error(session, "neg_unknown_socket", "blender_set_socket_default",
                       {"group": "MCP_ParametricGroup", "node": "Grid", "socket": "Nonexistent Socket", "value": 1.0},
                       "socket_not_found")
    await expect_error(session, "neg_unknown_node", "blender_set_socket_default",
                       {"group": "MCP_ParametricGroup", "node": "NoSuchNode", "socket": "Size X", "value": 1.0},
                       "node_not_found")
    await expect_error(session, "neg_incompatible_link", "blender_link_sockets",
                       {"group": "MCP_ParametricGroup", "from_node": "Grid", "from_socket": "Mesh",
                        "to_node": "XForm", "to_socket": "Translation"}, "incompatible_sockets")
    # A transaction that fails anywhere must be an error, and must leave the
    # node group exactly as it was.
    _ok, before_group = await expect_ok(session, "neg_rollback_snapshot", "blender_inspect_node_group",
                                        {"name": "MCP_ParametricGroup", "include_sockets": False})
    await expect_error(session, "neg_transactional_batch_rolls_back", "blender_batch_graph_edit",
                       {"group": "MCP_ParametricGroup",
                        "operations": [
                            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "RolledBack"},
                            {"op": "set_socket_default", "node": "Missing", "socket": "Size", "value": 1.0},
                        ]}, "graph_edit_rolled_back")
    _ok, after_group = await expect_ok(session, "neg_rollback_snapshot_after", "blender_inspect_node_group",
                                       {"name": "MCP_ParametricGroup", "include_sockets": False})
    record(
        "neg_rollback_left_the_group_unchanged",
        [node["name"] for node in before_group.get("nodes", [])] == [node["name"] for node in after_group.get("nodes", [])]
        and len(before_group.get("links", [])) == len(after_group.get("links", []))
        and "RolledBack" not in [node["name"] for node in after_group.get("nodes", [])],
        {"nodes": [node["name"] for node in after_group.get("nodes", [])]},
    )
    await expect_error(session, "neg_transactional_incompatible_options", "blender_batch_graph_edit",
                       {"group": "MCP_ParametricGroup", "transactional": True, "continue_on_error": True,
                        "operations": [{"op": "create_node", "node_type": "GeometryNodeMeshCube",
                                        "name": "Never"}]}, "incompatible_options")
    await expect_error(session, "neg_unknown_node_type", "blender_create_node",
                       {"group": "MCP_ParametricGroup", "node_type": "GeometryNodeDoesNotExist"}, "invalid_node_type")
    await expect_error(session, "neg_describe_unknown_node_type", "blender_describe_node_type",
                       {"node_type": "TotallyNotANode"}, "invalid_node_type")
    await expect_error(session, "neg_shape_key_topology", "blender_set_shape_key_coordinates",
                       {"object": "MCP_Sphere", "key_name": "Wide", "coordinates": [[0.0, 0.0, 0.0]]},
                       "topology_mismatch")
    await expect_error(session, "neg_shape_key_basis_protected", "blender_set_shape_key_coordinates",
                       {"object": "MCP_Sphere", "key_name": "Basis", "deltas": [[0.0, 0.0, 0.0]] * 482},
                       "basis_protected")
    await expect_error(session, "neg_unknown_shape_key", "blender_set_shape_key",
                       {"object": "MCP_Sphere", "key_name": "NoSuchKey", "value": 1.0}, "shape_key_not_found")
    await expect_error(session, "neg_driver_bad_data_path", "blender_add_driver",
                       {"owner_kind": "object", "owner_name": "MCP_Sphere", "data_path": "not_a_property",
                        "expression": "1.0"}, "invalid_data_path")
    await expect_error(session, "neg_driver_bad_expression", "blender_add_driver",
                       {"owner_kind": "object", "owner_name": "MCP_Sphere", "data_path": "scale", "index": 1,
                        "expression": "1 +"}, "invalid_expression")
    await expect_error(session, "neg_driver_python_exec", "blender_add_driver",
                       {"owner_kind": "object", "owner_name": "MCP_Sphere", "data_path": "scale", "index": 1,
                        "expression": "__import__('os').system('echo pwned')"}, "invalid_expression")
    await expect_error(session, "neg_driver_missing_target", "blender_add_driver",
                       {"owner_kind": "object", "owner_name": "MCP_Sphere", "data_path": "scale", "index": 2,
                        "expression": "ghost", "variables": [{"name": "ghost", "type": "SINGLE_PROP",
                         "targets": [{"id_type": "OBJECT", "id_name": "NoSuchObject", "data_path": "location"}]}]},
                       "target_not_found")
    await expect_error(session, "neg_driver_bad_owner", "blender_inspect_drivers",
                       {"owner_kind": "node_group", "owner_name": "NoSuchGroup"}, "node_group_not_found")
    await expect_error(session, "neg_remove_missing_driver", "blender_remove_driver",
                       {"owner_kind": "object", "owner_name": "MCP_Panel", "data_path": "rotation_euler", "index": 1},
                       "driver_not_found")
    # These two are rejected by the typed JSON schema before Blender is even called,
    # which is the strongest form of "explicit error": assert the schema message.
    await expect_error(session, "neg_batch_unknown_op", "blender_batch_graph_edit",
                       {"group": "MCP_ParametricGroup", "operations": [{"op": "explode"}]}, "validation error")
    await expect_error(session, "neg_primitive_unknown_kind", "blender_create_primitive",
                       {"kind": "dodecahedron"}, "validation error")
    await expect_error(session, "neg_mesh_bad_face_index", "blender_create_mesh_object",
                       {"name": "BadMesh", "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
                        "faces": [[0, 1, 9]]}, "invalid_argument")


async def check_auth_negative() -> None:
    """A wrong token must produce an auth error, and must never leak the token."""
    wrong_token_file = MCP_DIR / ".runtime" / "test_wrong_token"
    wrong_token = "0" * 64
    wrong_token_file.write_text(wrong_token + "\n", encoding="utf-8")
    async with open_session(BLENDER_MCP_TOKEN_FILE=str(wrong_token_file)) as (session, _proc):
        try:
            result = await asyncio.wait_for(session.call_tool("blender_health", {}), timeout=60)
        except Exception as exc:  # noqa: BLE001
            record("neg_wrong_token_is_rejected", "unauthorized" in str(exc), {"raised": str(exc)[:200]})
            return
        text = result_text(result)
        record(
            "neg_wrong_token_is_rejected",
            result.isError and "unauthorized" in text and wrong_token not in text,
            {"isError": result.isError, "error_text": text[:300], "token_leaked": wrong_token in text},
        )


async def check_dead_port_negative() -> None:
    """A dead bridge port must be a bridge-unavailable error, not a hang."""
    async with open_session(BLENDER_MCP_PORT="1") as (session, _proc):
        await expect_error(session, "neg_dead_bridge_port", "blender_health", {}, "bridge_unavailable")
        ok, probe = await expect_ok(session, "bridge_probe_reports_unreachable", "blender_bridge_probe", {})
        record("bridge_probe_reports_unreachable", probe.get("reachable") is False, probe)


def teardown(process: subprocess.Popen, token_file: Path) -> dict:
    """Stop ONLY the Blender this test started, with bounded waits."""
    info = {"pid": process.pid, "already_exited": process.poll() is not None}
    if process.poll() is None:
        # Ask nicely through our own protocol first.
        try:
            own_health = wait_for_bridge(TEST_PORT, token_file, 3.0)
            if not own_health or own_health.get("pid") != process.pid:
                raise OSError("bridge PID does not match the test process; skip protocol shutdown")
            token = token_file.read_text(encoding="utf-8").strip()
            with socket.create_connection(("127.0.0.1", TEST_PORT), timeout=3.0) as sock:
                sock.settimeout(3.0)
                sock.sendall((json.dumps({"id": "shutdown", "token": token, "command": "shutdown", "params": {}}) + "\n").encode())
                sock.recv(65536)
            info["shutdown_requested"] = True
        except OSError as exc:
            info["shutdown_requested"] = "failed: %s" % exc
        try:
            info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            info["terminate"] = True
            process.terminate()
            try:
                info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                info["kill"] = True
                process.kill()
                info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
    else:
        info["exit_code"] = process.returncode
    info["still_running"] = process.poll() is None
    return info


async def main() -> int:
    global BLENDER, TEST_PORT
    started = time.time()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = MCP_DIR / ".runtime" / "logs" / "stdio_blender_host.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    token_file = MCP_DIR / ".runtime" / "token"
    token_file.parent.mkdir(parents=True, exist_ok=True)

    if not PYTHON.is_file():
        record("venv_interpreter_present", False, {"expected": str(PYTHON)})
        return 1
    if not token_file.exists():
        # The server or bootstrap creates it; make sure it exists for the raw probe.
        config.ensure_token(token_file)

    try:
        BLENDER = find_blender()
    except RuntimeError as exc:
        record("blender_executable_found", False, {"error": str(exc)})
        return 1
    record("blender_executable_found", True, {"path": str(BLENDER)})

    # Refuse occupied ports, and verify the child's PID before any scene edits.
    # A test must never attach to or stop a user's existing Blender instance.
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", TEST_PORT))
            TEST_PORT = reservation.getsockname()[1]
    except OSError as exc:
        record("test_port_available", False, {"error": str(exc), "port": TEST_PORT})
        return 1

    # ---------------------------------------------------------------- host
    host_cmd = [
        str(BLENDER),
        "--background",
        "--factory-startup",
        "--python",
        str(BOOTSTRAP),
        "--",
        "--mcp-pump",                 # headless: timers never fire, pump manually
        "--mcp-pump-seconds", "600",  # safety net so a hung host cannot outlive the test
        "--mcp-port", str(TEST_PORT),
        "--mcp-token-file", str(token_file),
        "--mcp-quiet",
    ]
    env = dict(os.environ)
    env["BLENDER_USER_CONFIG"] = str(MCP_DIR / ".runtime" / "blender_config")
    env["TMP"] = str(MCP_DIR / ".runtime" / "tmp")
    env["TEMP"] = env["TMP"]
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(  # noqa: S603 - our own bundled Blender
            host_cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )
    record("blender_host_started", True, {"pid": process.pid, "log": str(log_path), "port": TEST_PORT})

    health = wait_for_bridge(TEST_PORT, token_file, BLENDER_START_TIMEOUT)
    record(
        "bridge_reachable_from_raw_client",
        health is not None and health.get("pid") == process.pid,
        {"blender_version": (health or {}).get("blender_version"), "pid": (health or {}).get("pid"),
         "host_pid": process.pid},
    )
    if health is None or health.get("pid") != process.pid:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""
        record("blender_host_log_tail", False, {"tail": tail})
        record("teardown", True, teardown(process, token_file))
        return 1

    context: dict = {}
    server_teardowns: list[dict] = []
    try:
        async with open_session() as (session, server_proc):
            record("mcp_initialize", True, {"server": str(SERVER_PY), "server_pid": getattr(server_proc, "pid", None)})
            context = await run_positive(session)
            await run_negatives(session)
        server_teardowns.append(last_teardown())
    except Exception as exc:  # noqa: BLE001
        record("mcp_stdio_session", False, {"exception": "%s: %s" % (type(exc).__name__, exc)})

    try:
        await check_auth_negative()
    except Exception as exc:  # noqa: BLE001
        record("neg_wrong_token_is_rejected", False, {"exception": "%s: %s" % (type(exc).__name__, exc)})
    server_teardowns.append(last_teardown())
    try:
        await check_dead_port_negative()
    except Exception as exc:  # noqa: BLE001
        record("neg_dead_bridge_port", False, {"exception": "%s: %s" % (type(exc).__name__, exc)})
    server_teardowns.append(last_teardown())

    record(
        "mcp_server_processes_stopped",
        all(not info.get("still_running") for info in server_teardowns if info),
        {"sessions": server_teardowns},
    )

    teardown_info = teardown(process, token_file)
    record(
        "teardown_only_own_process",
        teardown_info.get("still_running") is False,
        teardown_info,
    )

    failed = [entry["name"] for entry in RESULTS if not entry["ok"]]
    report = {
        "suite": "mcp_stdio_e2e",
        "blender_version": health.get("blender_version"),
        "blender_pid": process.pid,
        "bridge_port": TEST_PORT,
        "server_python": str(PYTHON),
        "duration_s": round(time.time() - started, 3),
        "checks_total": len(RESULTS),
        "checks_failed": failed,
        "ok": not failed,
        "context": context,
        "mcp_server_sessions": server_teardowns,
        "checks": RESULTS,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("STDIO::SUMMARY::" + json.dumps({"ok": report["ok"], "checks": report["checks_total"],
                                           "failed": failed, "report": str(REPORT_PATH)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
