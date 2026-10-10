"""Native Frame regressions; --live uses only official named MCP tools."""
from __future__ import annotations
import asyncio
import json
import sys
import uuid
from pathlib import Path
from datetime import timedelta
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
REPORT = ROOT / ".runtime" / "frame_acceptance.json"


class SchemaTests(unittest.TestCase):
    def test_existing_schemas_unchanged(self):
        from blender_mcp.tools import build_server, AuditedFastMCP
        from blender_mcp.tools import drivers, geometry_nodes, launcher, meta, operation_log, preview, scene, shape_keys
        class Stub:
            def call(self, *args, **kwargs):
                return {"audit_protocol": 1}
        old = AuditedFastMCP("baseline")
        for module in (meta, scene, geometry_nodes, shape_keys, drivers, launcher, preview, operation_log):
            module.register(old, Stub())
        baseline = {t.name: t.model_dump(exclude={"description"}) for t in asyncio.run(old.list_tools())}
        current = {t.name: t.model_dump(exclude={"description"}) for t in asyncio.run(build_server(Stub(), profile="all").list_tools())}
        self.assertEqual(len(baseline), 47)
        self.assertEqual(len(current), 93)
        self.assertTrue(all(current[k] == value for k, value in baseline.items()))

    def test_new_namespace_and_defaults(self):
        from blender_mcp.tools import build_server
        class Stub:
            def call(self, *args, **kwargs):
                return {}
        current = {t.name: t.inputSchema for t in asyncio.run(build_server(Stub(), profile="all").list_tools())}
        self.assertFalse(current["blender_node_frame_parent"]["properties"]["create_frame"]["default"])
        self.assertFalse(current["blender_node_frame_set_text"]["properties"]["overwrite"]["default"])
        self.assertFalse(current["blender_node_note_delete"]["properties"]["confirm"]["default"])
        self.assertEqual(current["blender_node_frame_parent"]["properties"]["nodes"]["maxItems"], 512)
        self.assertEqual(current["blender_node_frame_set_text"]["properties"]["body"]["maxLength"], 16384)

    def test_plugin_not_imported_by_server(self):
        from blender_mcp.tools import build_server
        build_server(profile="all")
        self.assertNotIn("bpy", sys.modules)
        self.assertFalse(any(key.startswith("bl_ext.") for key in sys.modules))


async def live():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    checks = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client:
            await client.initialize()
            async def call(name, args=None, error=None):
                reply = await client.call_tool(name, args or {})
                content = "\n".join(c.text for c in reply.content if c.type == "text")
                if error:
                    assert reply.isError and error in content, (error, content)
                    checks[name + ":" + error] = True
                    return content
                assert not reply.isError, content
                return json.loads(content)
            prefix = "FrameVerify_" + uuid.uuid4().hex[:8]
            group = prefix + "_GN"
            health = await call("blender_health")
            assert health["blender_version_tuple"] == [5, 2, 1]
            original = health["scene"]
            scene = prefix + "_Scene"
            output = {"checks": checks, "group": group, "scene": scene, "pid": health["pid"], "original_scene": original}
            try:
                await call("blender_create_case_scene", {"name": scene})
                await call("blender_create_node_group", {"name": group, "interface": [
                    {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "INPUT"},
                    {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}]})
                await call("blender_create_node", {"group": group, "node_type": "NodeGroupInput", "name": "Input", "location": [-400, 0]})
                await call("blender_create_node", {"group": group, "node_type": "GeometryNodeTransform", "name": "Process", "location": [0, 0]})
                await call("blender_create_node", {"group": group, "node_type": "NodeGroupOutput", "name": "Output", "location": [450, 0]})
                await call("blender_link_sockets", {"group": group, "from_node": "Input", "from_socket": "Geometry", "to_node": "Process", "to_socket": "Geometry"})
                await call("blender_link_sockets", {"group": group, "from_node": "Process", "from_socket": "Geometry", "to_node": "Output", "to_socket": "Geometry"})
                await call("blender_create_mesh_object", {"name": prefix + "_Object", "vertices": [[0,0,0],[1,0,0],[0,1,0]], "faces": [[0,1,2]]})
                await call("blender_attach_geometry_nodes_modifier", {"object": prefix + "_Object", "group": group})
                before = await call("blender_node_frame_layout", {"group": group})
                created = await call("blender_node_frame_create", {"group": group, "frame": "Core", "label": "Process", "location": [-80, 100], "color": [.2,.3,.5]})
                checks["created_native_without_plugin"] = created["created"]
                repeat = await call("blender_node_frame_create", {"group": group, "frame": "Core", "label": "Do not overwrite"})
                checks["create_idempotent_preserves_label"] = not repeat["created"] and repeat["label"] == "Process"
                await call("blender_node_frame_parent", {"group": group, "frame": "Missing", "nodes": ["Process"]}, error="node_not_found")
                await call("blender_node_frame_parent", {"group": group, "frame": "NeverCreated", "nodes": ["Process", "foreign"], "create_frame": True}, error="node_not_found")
                layout = await call("blender_node_frame_layout", {"group": group})
                checks["invalid_batch_no_partial_creation"] = not any(f["frame"] == "NeverCreated" for f in layout["frames"]) and next(n for n in layout["nodes"] if n["name"] == "Process")["parent"] is None
                await call("blender_node_frame_parent", {"group": group, "frame": "Core", "nodes": ["Process"]})
                repeat = await call("blender_node_frame_parent", {"group": group, "frame": "Core", "nodes": ["Process", "Process"]})
                checks["parent_idempotent"] = not repeat["changed"]
                await call("blender_node_frame_create", {"group": group, "frame": "Outer", "location": [-100, 200]})
                await call("blender_node_frame_parent", {"group": group, "frame": "Outer", "nodes": ["Core"]})
                await call("blender_node_frame_parent", {"group": group, "frame": "Core", "nodes": ["Outer"]}, error="parent_cycle")
                await call("blender_node_frame_parent", {"group": group, "frame": "Core", "nodes": ["Core"]}, error="parent_cycle")
                after = await call("blender_node_frame_layout", {"group": group})
                positions = lambda value: {n["name"]: (n["location_absolute"], n["width"]) for n in value["nodes"] if n["name"] in {"Input", "Process", "Output"}}
                checks["nested_world_positions_and_widths_preserved"] = positions(before) == positions(after)
                checks["computation_links_preserved"] = before["links"] == after["links"]
                await call("blender_node_frame_unparent", {"group": group, "frame": "Core", "nodes": ["Process"]})
                unparented = await call("blender_node_frame_layout", {"group": group})
                checks["unparent_world_positions_preserved"] = positions(before) == positions(unparented)
                repeat = await call("blender_node_frame_unparent", {"group": group, "frame": "Core", "nodes": ["Process"]})
                checks["unparent_idempotent"] = not repeat["changed"]
                text = await call("blender_node_frame_set_text", {"group": group, "frame": "Core", "body": "Purpose: transform geometry\nInput: mesh\nOutput: editable geometry"})
                again = await call("blender_node_frame_set_text", {"group": group, "frame": "Core", "body": "Updated\nSecond line"})
                checks["native_text_identity_stable"] = text["text"]["name"] == again["text"]["name"]
                await call("blender_node_frame_set_text", {"group": group, "frame": "Core", "body": ""}, error="empty_body")
                empty = await call("blender_node_frame_set_text", {"group": group, "frame": "Core", "body": "", "allow_empty": True})
                checks["explicit_empty_retains_text"] = empty["text"]["name"] == text["text"]["name"] and empty["text"]["body"] == ""
                await call("blender_node_frame_update", {"group": group, "frame": "Core", "label_size": 64, "label": "Core transform", "shrink": False, "width": 420, "height": 320})
                await call("blender_node_frame_update", {"group": group, "frame": "Core", "label": "Invalid", "label_size": 100}, error="invalid_value")
                unchanged = await call("blender_node_frame_get", {"group": group, "frame": "Core"})
                checks["invalid_properties_no_partial_write"] = unchanged["label"] == "Core transform"
                await call("blender_node_frame_get", {"group": "missing_exact_tree", "frame": "Core"}, error="node_group_not_found")
                await call("blender_node_frame_get", {"group": group, "frame": "Input"}, error="wrong_node_type")
                await call("blender_node_frame_set_text", {"group": group, "frame": "Core", "body": "x" * 16385}, error="validation")
                state = await call("blender_node_note_status")
                if not state["available"]:
                    checks["missing_extension_status"] = state["code"] == "dependency_missing" and state["blender_version_tuple"] == [5,2,1]
                    await call("blender_node_note_create", {"group": group, "frame": "Note"}, error="dependency_missing")
                output["extension_before"] = state
                output["final_layout"] = await call("blender_node_frame_layout", {"group": group})
                output["ok"] = all(checks.values())
                assert output["ok"], checks
            finally:
                await call("blender_switch_scene", {"name": original})
                REPORT.parent.mkdir(parents=True, exist_ok=True)
                REPORT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"ok": output["ok"], "checks": len(checks), "report": str(REPORT)}))


if __name__ == "__main__":
    if "--live" in sys.argv:
        asyncio.run(asyncio.wait_for(live(), timeout=240))
    else:
        unittest.main()
