"""Bounded named-MCP acceptance; never launches/stops Blender or executes bpy.

Requires the updated bridge in the existing instance. This runner explicitly
creates a new full-file checkpoint before confirmed fixture opens, then reopens
that checkpoint. No existing file is overwritten. The final filepath is the
checkpoint: this is reported, never hidden as an unchanged original path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent / "Project"
REPORT_PATH = ROOT / ".runtime" / "open_blend_acceptance.json"
REPORT = {"checks": {}, "responses": {}, "started_at": time.time()}


def asset_hashes():
    return {str(PROJECT / relative): hashlib.sha256((PROJECT / relative).read_bytes()).hexdigest()
            for relative in ("chairs/parametric_chair.blend", "models/petal_lamp.blend",
                             "models/petal_lamp_working.blend", "models/parametric_truss_bridge.blend")
            if (PROJECT / relative).is_file()}


def check(name, condition):
    REPORT["checks"][name] = bool(condition)
    assert condition, name


def record():
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")


async def main():
    async with asyncio.timeout(360):
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT), env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()

                async def call(name, args=None, errors=()):
                    reply = await asyncio.wait_for(session.call_tool(name, args or {}), timeout=60)
                    text = "\n".join(c.text for c in reply.content if c.type == "text")
                    if errors:
                        check(name + ":" + "|".join(errors), reply.isError and any(code in text for code in errors))
                        return text
                    assert not reply.isError, text
                    return reply.structuredContent or json.loads(text)

                health = await call("blender_health")
                REPORT["initial_health"] = health
                # Fail before fixture creation when an old resident bridge still runs.
                assert "open_blend" in health["commands"], "Run the updated MCP/blender/bootstrap.py in the existing Blender first"
                check("actual_5_2_1", health["blender_version_tuple"] == [5, 2, 1])
                initial_scenes = await call("blender_list_scenes", {"include_objects": True})
                initial_summary = await call("blender_scene_summary")

                async def original_object_snapshot():
                    # The depsgraph belongs to the active scene. Evaluating an
                    # object from an inactive scene can return stale transforms
                    # or unevaluated geometry, especially across file loading.
                    # Compare full object data in its owning scene, before and
                    # after loading, rather than dropping geometry assertions.
                    snapshots = {}
                    try:
                        for row in initial_scenes["scenes"]:
                            await call("blender_switch_scene", {"name": row["name"]})
                            await call("blender_update_depsgraph")
                            snapshots[row["name"]] = {
                                name: await call("blender_object_info", {"name": name})
                                for name in sorted(row["object_names"])}
                    finally:
                        await call("blender_switch_scene", {"name": health["scene"]})
                    return snapshots

                object_before = await original_object_snapshot()
                REPORT["original_objects_before"] = object_before
                group_before = {row["name"]: await call("blender_inspect_node_group", {"name": row["name"]}) for row in initial_summary["node_groups"]}
                REPORT["original_asset_hashes"] = asset_hashes()
                prefix = "OpenVerify_" + uuid.uuid4().hex[:8]
                scene, group, obj = prefix + "_Scene", prefix + "_GN", prefix + "_Object"
                directory = "算法复现/00_算法基础/_MCP打开验收_" + prefix
                fixture = directory + "/fixture.blend"
                checkpoint = directory + "/session_checkpoint.blend"
                checkpoint_path = str(PROJECT / checkpoint)
                restore_needed = False
                try:
                    fresh = await call("blender_create_case_scene", {"name": scene})
                    check("new_case_genuinely_empty", fresh["empty"]["empty"] and fresh["empty"]["objects"] == 0 and fresh["empty"]["world"] is None)
                    await call("blender_create_mesh_object", {"name": obj, "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]})
                    await call("blender_create_node_group", {"name": group, "with_input_output": False, "interface": [
                        {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "INPUT"},
                        {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}]})
                    for kind, name, xy in (("NodeGroupInput", "Input", [-400, 0]), ("GeometryNodeTransform", "Process", [0, 0]), ("NodeGroupOutput", "Output", [400, 0])):
                        await call("blender_create_node", {"group": group, "node_type": kind, "name": name, "location": xy})
                    for source, dest in (("Input", "Process"), ("Process", "Output")):
                        await call("blender_link_sockets", {"group": group, "from_node": source, "from_socket": "Geometry", "to_node": dest, "to_socket": "Geometry"})
                    await call("blender_attach_geometry_nodes_modifier", {"object": obj, "group": group})
                    built_mesh = (await call("blender_object_info", {"name": obj}))["mesh"]
                    REPORT["fixture_graph"] = await call("blender_inspect_node_group", {"name": group})
                    check("fixture_graph_generates_triangle", built_mesh["evaluated"]["vertices"] == 3 and built_mesh["evaluated"]["polygons"] == 1)
                    saved = await call("blender_save_scene", {"scene": scene, "relative_path": fixture, "compress": True})
                    target = saved["path"]
                    inventory = await call("blender_inspect_blend_file", {"relative_path": fixture})
                    check("fixture_contains_only_case", inventory["scenes"] == [scene] and inventory["objects"] == [obj] and group in inventory["inventories"].get("node_groups", []))
                    check("inspect_did_not_open", inventory["opened_into_live_data"] is False and (await call("blender_health"))["blend_filepath"] == health["blend_filepath"])
                    await call("blender_save_scene", {"scene": scene, "relative_path": fixture}, errors=("file_exists",))
                    await call("blender_switch_scene", {"name": health["scene"]})
                    check("original_objects_unchanged_after_case_creation", object_before == await original_object_snapshot())
                    check("original_groups_unchanged_after_case_creation", group_before == {name: await call("blender_inspect_node_group", {"name": name}) for name in group_before})
                    # An explicit, separate save tool call: open_blend never saves.
                    checkpoint_result = await call("blender_save_blend", {"relative_path": checkpoint, "compress": True})
                    REPORT["checkpoint"] = checkpoint_result
                    REPORT["initial_filepath"] = health["blend_filepath"]
                    REPORT["checkpoint_filepath"] = checkpoint_path
                    before = await call("blender_health")
                    baseline_target_hash = hashlib.sha256(Path(target).read_bytes()).hexdigest()
                    hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (target, checkpoint_path)}
                    await call("blender_open_blend", {"path": target}, errors=("unsaved_changes", "confirmation_required"))
                    check("unconfirmed_did_not_replace", (await call("blender_health"))["blend_filepath"] == before["blend_filepath"])
                    for path, codes in (("relative.blend", ("invalid_path",)), (str(ROOT / "outside.blend"), ("invalid_path", "path_outside_project")), (str(PROJECT), ("invalid_path", "not_a_file")), (str(PROJECT / "absent_" / "not_here.blend"), ("file_not_found",)), (str(PROJECT / "wrong.txt"), ("invalid_path",))):
                        await call("blender_open_blend", {"path": path, "confirm_replace_current": True}, errors=codes)
                    corrupt = PROJECT / directory / "corrupt.blend"
                    corrupt.write_bytes(b"not a Blender file; dedicated corruption fixture")
                    await call("blender_open_blend", {"path": str(corrupt), "confirm_replace_current": True}, errors=("load_failed", "blend_inspect_failed", "invalid_blend"))
                    check("corrupt_file_did_not_replace", (await call("blender_health"))["blend_filepath"] == before["blend_filepath"])
                    restore_needed = True
                    opened = await call("blender_open_blend", {"path": target, "confirm_replace_current": True})
                    REPORT["responses"]["opened"] = opened
                    loaded_health = await call("blender_health")
                    check("same_instance_target_is_current", loaded_health["pid"] == health["pid"] and Path(loaded_health["blend_filepath"]).resolve() == Path(target).resolve())
                    check("loaded_case_excludes_old_scenes", [row["name"] for row in (await call("blender_list_scenes"))["scenes"]] == [scene])
                    await call("blender_ping")
                    commands = await call("blender_list_commands")
                    names = [row["name"] if isinstance(row, dict) else row for row in commands["commands"]]
                    check("bridge_survives_load", "open_blend" in names)
                    await call("blender_inspect_node_group", {"name": group})
                    original_bounds = (await call("blender_object_info", {"name": obj}))["mesh"]
                    changed = await call("blender_set_socket_default", {"group": group, "node": "Process", "socket": "Translation", "value": [0.25, 0, 0]})
                    REPORT["responses"]["edit"] = changed
                    new_bounds = (await call("blender_object_info", {"name": obj}))["mesh"]
                    REPORT["evaluated_before_edit"] = original_bounds
                    REPORT["evaluated_after_edit"] = new_bounds
                    check("loaded_group_edit_changes_evaluated_geometry",
                          new_bounds["evaluated"]["vertices"] == 3 and
                          all(abs(new_bounds["bounds"][key][0] - original_bounds["bounds"][key][0] - 0.25) < 1e-6 and
                              new_bounds["bounds"][key][1:] == original_bounds["bounds"][key][1:]
                              for key in ("min", "max")))
                    check("edit_not_implicitly_saved", hashlib.sha256(Path(target).read_bytes()).hexdigest() == baseline_target_hash)
                    reopened = await call("blender_open_blend", {"path": target, "confirm_replace_current": True, "load_ui": True})
                    REPORT["responses"]["load_ui_true"] = reopened
                    check("load_ui_true_same_instance", (await call("blender_health"))["pid"] == health["pid"])
                    before_inventory = await call("blender_health")
                    await call("blender_inspect_blend_file", {"relative_path": checkpoint})
                    after_inventory = await call("blender_health")
                    check("read_only_inspect_still_read_only", before_inventory["blend_filepath"] == after_inventory["blend_filepath"] and before_inventory["scene"] == after_inventory["scene"])
                    check("target_and_checkpoint_files_unchanged", all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items()))
                finally:
                    if restore_needed:
                        REPORT["restored"] = await call("blender_open_blend", {"path": checkpoint_path, "confirm_replace_current": True, "load_ui": True})
                        restored = await call("blender_list_scenes", {"include_objects": True})
                        before_rows = {r["name"]: sorted(r["object_names"]) for r in initial_scenes["scenes"]}
                        after_rows = {r["name"]: sorted(r["object_names"]) for r in restored["scenes"] if r["name"] in before_rows}
                        check("all_original_scene_object_rosters_restored", before_rows == after_rows)
                        object_after = await original_object_snapshot()
                        REPORT["original_objects_after"] = object_after
                        check("original_objects_restored", object_before == object_after)
                        check("original_groups_restored", group_before == {name: await call("blender_inspect_node_group", {"name": name}) for name in group_before})
                    else:
                        await call("blender_switch_scene", {"name": health["scene"]})
                    check("existing_asset_files_unchanged", asset_hashes() == REPORT["original_asset_hashes"])
                    REPORT["final_health"] = await call("blender_health")
                    record()
                REPORT["ok"] = all(REPORT["checks"].values())
                record()
                print(json.dumps({"ok": REPORT["ok"], "checks": len(REPORT["checks"]), "report": str(REPORT_PATH)}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        REPORT.update(ok=False, error=str(exc))
        record()
        raise
