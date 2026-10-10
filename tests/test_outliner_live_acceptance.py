"""Named MCP acceptance on unique, inactive fixture data; never saves a .blend.

Run manually after the management bridge commands have been loaded. The gate
refuses to create any fixture when health does not advertise the complete set.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from stdio_pipe_transport import mcp_stdio_session

REPORT = ROOT / ".runtime" / "outliner_live_acceptance.json"
REQUIRED = {
    "outliner_hierarchy", "outliner_ensure_path", "outliner_link_objects",
    "outliner_move_objects", "outliner_unlink_objects", "outliner_set_visibility",
    "outliner_collection_create", "outliner_collection_rename", "outliner_collection_move",
    "outliner_collection_delete", "outliner_object_create", "outliner_object_rename",
    "outliner_object_delete", "outliner_collection_display", "outliner_object_display",
}


async def verify():
    report = {"kind": "named_mcp_real_blender", "checks": [], "fixture_only": True,
              "saved_blend": False, "ok": False, "status": "in_progress",
              "fixture_created": False}
    prefix = "MCPOutliner_" + uuid.uuid4().hex[:8]
    scene = prefix + "_Scene"
    chapter = prefix + "/" + prefix + "_Chapter"
    path_a, path_b = chapter + "/CaseA_" + prefix, chapter + "/CaseB_" + prefix
    a, b, group = prefix + "_A", prefix + "_B", prefix + "_GN"
    report.update(fixture_scene=scene, fixture_objects=[a, b], fixture_node_group=group)

    def record():
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    record()  # Start a fresh report; failures must not reuse a previous run.
    layer = "ViewLayer"
    env = dict(os.environ, BLENDER_MCP_PROFILE="all")
    env.pop("BLENDER_MCP_PORT", None)
    env.pop("BLENDER_MCP_TOKEN_FILE", None)
    env["BLENDER_MCP_AUDIT_DIR"] = str(ROOT / ".runtime" / "outliner_live_audit")
    async with mcp_stdio_session(sys.executable, [str(ROOT / "server.py")], env=env, cwd=ROOT,
                                stderr_path=ROOT / ".runtime" / "outliner_live.stderr.log") as (session, _):
        async def call(tool_name, expected_error=None, **args):
            result = await asyncio.wait_for(session.call_tool("blender_" + tool_name, args), 50)
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            if expected_error:
                assert result.isError and expected_error in text, (tool_name, text)
                return text
            assert not result.isError, (tool_name, text)
            return result.structuredContent or json.loads(text)

        def check(label, condition):
            assert condition, label
            report["checks"].append(label)
            record()

        async def tree():
            return await call("outliner_hierarchy", scene=scene)

        async def fixture_tree():
            # File-wide GN statistics can change while another task is working.
            # Compare the complete hierarchy of our own isolated scene instead.
            listing = await tree()
            return {key: listing[key] for key in ("collections", "objects") if key in listing}

        async def object_info(name):
            return await call("object_info", name=name, include_evaluated=False)

        before = await call("health")
        missing = sorted(REQUIRED - set(before["commands"]))
        report.update(pid=before["pid"], version=before["blender_version"], missing_commands=missing)
        record()
        if missing:
            raise RuntimeError("bridge_refresh_required: no fixture created; missing=" + ",".join(missing))
        check("full bridge command gate", True)
        report.update(pid=before["pid"], version=before["blender_version"], fixture_scene=scene)
        original_file = Path(before["blend_filepath"]) if before["blend_filepath"] else None
        original_hash = hashlib.sha256(original_file.read_bytes()).hexdigest() if original_file else None
        report.update(original_file=str(original_file) if original_file else None,
                      original_file_sha256=original_hash, original_scene=before["scene"])
        record()
        await call("create_case_scene", name=scene, activate=False)
        report["fixture_created"] = True
        record()
        discovered = await tree()
        check("explicit view-layer discovery", bool(discovered["view_layers"]))
        layer = discovered["view_layers"][0]
        h = await call("outliner_ensure_path", scene=scene, path=path_a)
        check("root chapter case created", len(h["created"]) == 3)
        repeated = await call("outliner_ensure_path", scene=scene, path=path_a)
        check("ensure idempotent no suffix", not repeated["created"] and repeated["idempotent"])
        await call("outliner_ensure_path", scene=scene, path=path_b)
        for name in (a, b):
            await call("outliner_object_create", scene=scene, path="", name=name, type="MESH",
                       vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
        await call("create_node_group", name=group, with_input_output=False, geometry_output=False)
        source = (await call("create_node", group=group, node_type="ShaderNodeValue", name=prefix + "_Value"))["node"]
        destination = (await call("create_node", group=group, node_type="ShaderNodeMath", name=prefix + "_Math"))["node"]
        await call("link_sockets", group=group, from_node=source["name"],
                   from_socket=source["outputs"][0]["identifier"], to_node=destination["name"],
                   to_socket=destination["inputs"][0]["identifier"])
        await call("attach_geometry_nodes_modifier", object=a, group=group, modifier_name=prefix + "_Modifier")
        graph_snapshot = await call("inspect_node_group", name=group)
        check("GN fixture contains a real connection", bool(graph_snapshot["links"]))
        snapshots = {name: await object_info(name) for name in (a, b)}
        await call("outliner_link_objects", scene=scene, path=path_a, objects=[a, b])
        repeated = await call("outliner_link_objects", scene=scene, path=path_a, objects=[a, b])
        check("repeated link skipped", sorted(repeated["skipped"]) == sorted([a, b]))
        await call("outliner_move_objects", scene=scene, path=path_b, objects=[b], unlink_from=["", path_a])
        await call("outliner_unlink_objects", scene=scene, path=path_a, objects=[a])
        check("data transforms modifiers GN unchanged", all([await object_info(name) == snapshots[name] for name in (a, b)]))
        check("GN nodes sockets and connections unchanged", await call("inspect_node_group", name=group) == graph_snapshot)
        listing = await tree()
        rows = {row["path"]: row for row in listing["collections"]}
        check("hierarchy and direct objects", rows[path_b]["objects"] == [b] and rows[path_a]["objects"] == [])
        await call("outliner_unlink_objects", expected_error="orphan_object", scene=scene, path=path_b, objects=[b])
        orphaned = await call("outliner_unlink_objects", scene=scene, path=path_b, objects=[b], confirm_orphan=True)
        check("explicit orphan keeps object", bool(orphaned["warnings"]) and (await object_info(b))["name"] == b)
        await call("outliner_link_objects", scene=scene, path=path_b, objects=[b])
        stable = await fixture_tree()
        for args, error in [({"scene": "__absent__", "path": path_a}, "scene_not_found"),
                            ({"scene": scene, "path": prefix + "/NoParent/NoCase", "create_missing": False}, "collection_not_found"),
                            ({"scene": scene, "path": prefix + "/New/CaseA_" + prefix}, "collection_name_conflict")]:
            await call("outliner_ensure_path", expected_error=error, **args)
        await call("outliner_link_objects", expected_error="object_not_found", scene=scene, path=path_a, objects=[a, "__absent__"])
        await call("outliner_link_objects", expected_error="duplicate_object", scene=scene, path=path_a, objects=[a, a])
        await call("outliner_collection_move", expected_error="cycle_hierarchy", scene=scene, path=prefix, parent_path=path_b)
        check("errors have no partial effects", await fixture_tree() == stable)
        await call("outliner_collection_create", scene=scene, parent_path=prefix, name=prefix + "_Empty")
        await call("outliner_collection_rename", scene=scene, path=prefix + "/" + prefix + "_Empty", name=prefix + "_Renamed")
        await call("outliner_collection_move", scene=scene, path=prefix + "/" + prefix + "_Renamed", parent_path=path_a)
        empty_path = path_a + "/" + prefix + "_Renamed"
        await call("outliner_collection_delete", expected_error="confirmation_required", scene=scene, path=empty_path, confirm_delete=False)
        await call("outliner_collection_delete", scene=scene, path=empty_path, confirm_delete=True)
        for state in (True, False):
            visibility = await call("outliner_set_visibility", scene=scene, path=path_b,
                                    hide_viewport=state, hide_render=state)
            check(f"base visibility={state} readback", visibility["after"] == {
                "hide_viewport": state, "hide_render": state})
        for tool, args in [("outliner_collection_display", {"path": path_b}), ("outliner_object_display", {"objects": [b]})]:
            flags = ["hidden", "disable_viewport", "hide_render"]
            if tool == "outliner_collection_display":
                flags.append("exclude")
            for flag in flags:
                for state in (True, False):
                    result = await call(tool, scene=scene, view_layer=layer, **args, **{flag: state})
                    readback = result["after"] if tool == "outliner_collection_display" else result["after"][b]
                    check(f"{tool}.{flag}={state} readback", readback[flag] is state)
        check("display switches roundtrip", (await object_info(b)) == snapshots[b])
        await call("outliner_object_rename", scene=scene, object=b, name=b + "_Renamed")
        renamed = await object_info(b + "_Renamed")
        check("rename keeps underlying data", renamed["data"] == snapshots[b]["data"])
        await call("outliner_object_delete", expected_error="confirmation_required", scene=scene, objects=[b + "_Renamed"], confirm_delete=False)
        await call("outliner_object_delete", scene=scene, objects=[b + "_Renamed"], confirm_delete=True)
        await call("object_info", expected_error="object_not_found", name=b + "_Renamed", include_evaluated=False)
        check("delete only requested object", (await object_info(a))["modifiers"] == snapshots[a]["modifiers"])
        await call("outliner_move_objects", scene=scene, path=path_a, objects=[a], unlink_from=[""])
        before_delete = await tree()
        before_rows = {row["path"]: row for row in before_delete["collections"]}
        check("GN host belongs only to deleted subtree", before_rows[path_a]["objects"] == [a]
              and a not in before_rows[""]["objects"])
        await call("outliner_collection_delete", scene=scene, path=prefix, confirm_delete=True, recursive=True)
        check("collection recursive delete preserves object and GN", await object_info(a) == snapshots[a])
        after_delete = await tree()
        after_rows = {row["path"]: row for row in after_delete["collections"]}
        check("recursive delete relinks host to scene root", a in after_rows[""]["objects"]
              and prefix not in after_rows)
        check("GN graph preserved through collection deletion", await call("inspect_node_group", name=group) == graph_snapshot)
        after = await call("health")
        check("original filepath and active scene kept", before["scene"] == after["scene"] and before["blend_filepath"] == after["blend_filepath"])
        check("original disk file not written", not original_file or hashlib.sha256(original_file.read_bytes()).hexdigest() == original_hash)
        report.update(ok=True, status="passed", fixtures_left_unsaved=True, original_file_sha256=original_hash)
    return report


if __name__ == "__main__":
    try:
        result = asyncio.run(verify())
        REPORT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": result["ok"], "checks": len(result["checks"])}))
    except Exception as exc:
        issue = exc
        while isinstance(issue, BaseExceptionGroup) and len(issue.exceptions) == 1:
            issue = issue.exceptions[0]
        try:
            failed = json.loads(REPORT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failed = {}
        failed.update(ok=False, error=str(issue), status="blocked" if str(issue).startswith("bridge_refresh_required:") else "failed")
        REPORT.write_text(json.dumps(failed, indent=2, ensure_ascii=False), encoding="utf-8")
        if str(issue).startswith("bridge_refresh_required:"):
            print(json.dumps({"ok": False, "status": "blocked", "error": str(issue)}))
            sys.exit(2)
        raise
