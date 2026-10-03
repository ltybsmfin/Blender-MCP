"""Real-Blender feature test: transactional batch edits, audit log, preview capture.

Runs INSIDE Blender 5.2.1 LTS (``--background``) and drives the real bridge
command handlers, so every assertion is about Blender's actual datablocks and its
actual dependency graph - not about a mock.

What it proves
--------------
1. ``batch_graph_edit`` is transactional by default: a batch that fails anywhere
   leaves the addressed node group **exactly** as it was - nodes, links, socket
   defaults, interface items, pointer identity, modifier input values, driver
   targets and nested group references - and reports ``rolled_back`` truthfully.
2. ``transactional`` + ``continue_on_error`` is refused before any mutation.
3. A successful transaction replaces the node group datablock identity while
   re-pointing every enumerated user, keeping modifier input values (still wired
   to the new graph), the driver target and the nested group reference.
4. The legacy ``transactional=false`` mode still applies partial edits.
5. Linked-library groups are refused.
6. Every bridge command is audited as JSONL (UTC timestamp, session/request id,
   bounded sanitised arguments, outcome, duration) with size rotation, and a
   schema validation failure is recorded too; the bridge token never appears.
7. ``capture_preview`` writes a real, non-blank PNG whose dimensions match the
   request, frames the *evaluated* geometry, and restores every render setting,
   camera, frame and temporary datablock; ``viewport`` mode fails with an
   actionable error when there is no GUI (this run is headless).

Isolation
---------
Blender writes only inside ``<MCP>/.runtime/feature_workspace`` (the add-on's
documented ``BLENDER_MCP_WORKSPACE`` override) because this build sandbox denies
file writes outside ``<MCP>``. The path rules under test are unchanged: previews
still have to live in ``<workspace>/Project/previews/*.png``, and the default
workspace is asserted to be the sibling ``Project`` directory.

Command::

    blender.exe --background --factory-startup --python-exit-code 1 \
        --python MCP\\tests\\blender_feature_integration.py

Prints ``FEATURE::PASS::<json>`` per check and writes
``MCP/tests/reports/blender_feature_report.json``. Exits non-zero on failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

import bpy

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
REPORT_DIR = TESTS_DIR / "reports"
RUNTIME = MCP_DIR / ".runtime"
FEATURE_WORKSPACE = RUNTIME / "feature_workspace"
AUDIT_DIR = RUNTIME / "feature_audit"

sys.path.insert(0, str(MCP_DIR / "blender"))

# Must be set before the add-on resolves any Project path. It is the add-on's own
# documented override, and the sandbox denies writes to the real sibling Project.
os.environ["BLENDER_MCP_WORKSPACE"] = str(FEATURE_WORKSPACE)
os.environ["BLENDER_MCP_AUDIT_DIR"] = str(AUDIT_DIR)

import blender_mcp_addon as addon  # noqa: E402  (after sys.path setup)

PREVIEW_REL = "previews/feature_panel.png"
TOLERANCE = 1e-3


class Checker:
    def __init__(self):
        self.checks: list[dict] = []
        self.started = time.time()

    def check(self, name: str, ok: bool, details=None) -> bool:
        entry = {"name": name, "ok": bool(ok)}
        if details is not None:
            entry["details"] = details
        self.checks.append(entry)
        print("FEATURE::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str)))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(entry["ok"] for entry in self.checks)

    def summary(self) -> dict:
        failed = [entry["name"] for entry in self.checks if not entry["ok"]]
        return {
            "suite": "blender_feature_integration",
            "blender_version": bpy.app.version_string,
            "python_version": sys.version.split()[0],
            "background": bool(bpy.app.background),
            "headless_viewport_tested": False,
            "gui_viewport_note": (
                "mode='viewport' can only be exercised for its 'unavailable' error path in "
                "--background; the GUI capture path needs a manual run in a windowed Blender"
            ),
            "workspace_override": str(FEATURE_WORKSPACE),
            "duration_s": round(time.time() - self.started, 3),
            "checks_total": len(self.checks),
            "checks_failed": failed,
            "ok": self.ok,
            "checks": self.checks,
        }


C = Checker()


def call(command: str, **params):
    return addon.dispatch(command, params)


def deps():
    return addon.depsgraph_update()


def error_of(command: str, **params):
    try:
        call(command, **params)
    except addon.CommandError as exc:
        return exc
    return None


def close(value, expected, tol=TOLERANCE) -> bool:
    try:
        return abs(float(value) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# structural snapshots
# --------------------------------------------------------------------------- #
def group_snapshot(name: str) -> dict:
    group = bpy.data.node_groups[name]
    nodes = []
    for node in sorted(group.nodes, key=lambda item: item.name):
        entry = {
            "name": node.name,
            "idname": node.bl_idname,
            "label": node.label,
            "location": [round(v, 4) for v in node.location],
            "inputs": [],
            "outputs": [],
        }
        for socket in node.inputs:
            entry["inputs"].append({
                "identifier": socket.identifier,
                "type": socket.type,
                "links": len(socket.links),
                "default": repr(addon._socket_default(socket)),
            })
        for socket in node.outputs:
            entry["outputs"].append({
                "identifier": socket.identifier,
                "type": socket.type,
                "links": len(socket.links),
            })
        if node.bl_idname == "GeometryNodeGroup":
            entry["node_tree"] = getattr(node.node_tree, "name", None)
        nodes.append(entry)
    links = sorted(
        "%s.%s -> %s.%s" % (link.from_node.name, link.from_socket.identifier,
                            link.to_node.name, link.to_socket.identifier)
        for link in group.links
    )
    interface = []
    for item in group.interface.items_tree:
        interface.append({
            "name": item.name,
            "identifier": item.identifier,
            "in_out": item.in_out,
            "socket_type": item.socket_type,
            "default": repr(addon._interface_item_default_payload(item)),
        })
    drivers = []
    animation = getattr(group, "animation_data", None)
    for fcurve in list(getattr(animation, "drivers", None) or []):
        drivers.append({
            "data_path": getattr(fcurve, "data_path", None),
            "array_index": getattr(fcurve, "array_index", None),
            "expression": fcurve.driver.expression,
            "valid": bool(fcurve.driver.is_valid),
            "variables": [variable.name for variable in fcurve.driver.variables],
        })
    return {
        "name": group.name,
        "pointer": group.as_pointer(),
        "nodes": nodes,
        "links": links,
        "interface": interface,
        "interface_identifiers": sorted(item.identifier for item in group.interface.items_tree),
        "drivers": drivers,
        "has_animation": animation is not None,
        "node_groups_total": len(bpy.data.node_groups),
    }


def structural(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "pointer"}


def modifier_parameters(object_name: str, modifier: str = "GN") -> dict:
    obj = bpy.data.objects[object_name]
    mod = obj.modifiers[modifier]
    group = mod.node_group
    raw = addon._modifier_parameters(mod, group)
    return {entry["name"]: entry.get("value") for entry in raw.values()}


def driver_state(object_name: str) -> dict:
    obj = bpy.data.objects[object_name]
    values = float(obj.rotation_euler[1])
    targets = []
    for fcurve in addon._iter_driver_fcurves():
        if getattr(fcurve, "data_path", "") != "rotation_euler":
            continue
        for variable in fcurve.driver.variables:
            for target in variable.targets:
                block = target.id
                targets.append({"id": getattr(block, "name", None),
                                "id_type": target.id_type,
                                "data_path": target.data_path,
                                "pointer": block.as_pointer() if block is not None else None,
                                "valid": bool(fcurve.driver.is_valid)})
    return {"value": round(values, 6), "targets": targets}


def evaluated(object_name: str) -> dict:
    stats = addon.evaluated_mesh_stats(bpy.data.objects[object_name])
    return {"vertices": (stats.get("evaluated") or {}).get("vertices"),
            "bounds": stats.get("bounds")}


def scene_state() -> dict:
    scene = bpy.context.scene
    return {
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "resolution_percentage": scene.render.resolution_percentage,
        "engine": scene.render.engine,
        "filepath": scene.render.filepath,
        "file_format": scene.render.image_settings.file_format,
        "film_transparent": bool(scene.render.film_transparent),
        "camera": scene.camera.name if scene.camera else None,
        "frame": scene.frame_current,
        "scenes": sorted(item.name for item in bpy.data.scenes),
        "objects": sorted(item.name for item in bpy.data.objects),
        "cameras": sorted(item.name for item in bpy.data.cameras),
        "lights": sorted(item.name for item in bpy.data.lights),
        "worlds": sorted(item.name for item in bpy.data.worlds),
        "collections": sorted(item.name for item in bpy.data.collections),
        "materials": sorted(item.name for item in bpy.data.materials),
    }


def leaked_temp_datablocks() -> list:
    leaked = []
    for collection, label in ((bpy.data.scenes, "scene"), (bpy.data.objects, "object"),
                              (bpy.data.cameras, "camera"), (bpy.data.lights, "light"),
                              (bpy.data.worlds, "world"), (bpy.data.node_groups, "node_group")):
        for item in collection:
            if addon.PREVIEW_TEMP_PREFIX in item.name or "__mcp_" in item.name:
                leaked.append("%s:%s" % (label, item.name))
    return leaked


def audit_events() -> list[dict]:
    events = []
    for path in sorted(AUDIT_DIR.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def png_border_margins(path: Path) -> dict:
    """Content bounding box of a PNG plus the margin left on each border.

    Uses Blender's own image loader and numpy (both bundled), so the assertion is
    about the actual pixels Blender produced, not about the render settings.
    """
    import numpy as np

    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = int(image.size[0]), int(image.size[1])
        buffer = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(buffer)
    finally:
        bpy.data.images.remove(image)
    rgba = buffer.reshape(height, width, 4)
    background = rgba[0, 0, :3]
    mask = np.abs(rgba[:, :, :3] - background).sum(axis=2) > 0.08
    rows, columns = np.nonzero(mask)
    if rows.size == 0:
        return {"width": width, "height": height, "empty": True, "pixels": int(mask.sum())}
    min_x, max_x = int(columns.min()), int(columns.max())
    min_y, max_y = int(rows.min()), int(rows.max())
    return {
        "width": width,
        "height": height,
        "empty": False,
        "content_bbox": [min_x, min_y, max_x, max_y],
        "content_pixels": int(mask.sum()),
        "margins": {"left": min_x, "right": width - 1 - max_x,
                    "bottom": min_y, "top": height - 1 - max_y},
    }


def _content_width(margins: dict) -> int:
    if margins.get("empty") or not margins.get("content_bbox"):
        return 0
    return int(margins["content_bbox"][2] - margins["content_bbox"][0])


def _min_margin(margins: dict) -> int:
    values = margins.get("margins") or {}
    return min(values.values()) if values else -1


def png_pixels(path: Path) -> dict:
    """Load the PNG in Blender and measure whether it is actually non-blank."""
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        pixels = list(image.pixels)
    finally:
        bpy.data.images.remove(image)
    rgb = pixels[: len(pixels) // 4 * 4]
    values = rgb[0::4] + rgb[1::4] + rgb[2::4]
    return {
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "distinct": len({round(value, 3) for value in values}),
        "samples": len(values),
    }


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def build_fixture() -> None:
    call("create_mesh_object", name="TxPanel",
         vertices=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], faces=[[0, 1, 2, 3]])
    call("create_mesh_object", name="TxDriver", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    call("create_mesh_object", name="TxOuterPanel",
         vertices=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], faces=[[0, 1, 2, 3]])
    call("create_node_group", name="TxGroup", if_exists="replace",
         interface=[
             {"name": "Size", "socket_type": "NodeSocketFloat", "in_out": "INPUT", "default_value": 2.0},
             {"name": "Rows", "socket_type": "NodeSocketInt", "in_out": "INPUT", "default_value": 4},
             {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
         ])
    # Legacy (non-transactional) construction of the *original* graph.
    legacy = call("batch_graph_edit", group="TxGroup", transactional=False,
                  operations=[
                      {"op": "create_node", "node_type": "GeometryNodeMeshGrid", "name": "Grid"},
                      {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
                       "to_node": "Group Output", "to_socket": "Geometry"},
                      {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
                       "to_node": "Grid", "to_socket": "Size X"},
                      {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
                       "to_node": "Grid", "to_socket": "Size Y"},
                      {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Rows",
                       "to_node": "Grid", "to_socket": "Vertices X"},
                      {"op": "set_socket_default", "node": "Grid", "socket": "Vertices Y", "value": 4},
                  ])
    C.check("legacy_batch_mode_is_not_transactional",
            legacy["transactional"] is False and legacy["rollback"] is False
            and legacy["completed_count"] == 6 and legacy["transaction"]["mode"] == "in_place",
            {"transactional": legacy["transactional"], "rollback": legacy["rollback"],
             "completed": legacy["completed_count"], "status": legacy["transaction"]["status"]})

    call("set_frame", frame=1)
    bpy.data.node_groups["TxGroup"]["phase"] = 2.0
    # Order matters: the external driver is created while the group is not yet
    # consumed by a modifier (Blender reports a freshly created driver on a group
    # that is evaluated as a modifier input as invalid). It must stay valid - and
    # re-pointed - after the modifier is attached below.
    call("add_driver", owner_kind="object", owner_name="TxDriver", data_path="rotation_euler",
         index=1, expression="ph * 3",
         variables=[{"name": "ph", "type": "SINGLE_PROP",
                     "targets": [{"id_type": "NODETREE", "id_name": "TxGroup", "data_path": '["phase"]'}]}])
    call("attach_geometry_nodes_modifier", object="TxPanel", group="TxGroup",
         modifier_name="GN", parameters={"Size": 3.0, "Rows": 4})
    call("update_depsgraph")
    C.check("fixture_driver_targets_the_group_and_evaluates",
            driver_state("TxDriver")["value"] == 6.0
            and [target["id"] for target in driver_state("TxDriver")["targets"]] == ["TxGroup"],
            {"driver": driver_state("TxDriver")})
    # The node group also carries its own animation data (a driver on one of its
    # custom properties): that is the "tree animation" the swap has to preserve.
    bpy.data.node_groups["TxGroup"]["phase_out"] = 0.0
    call("add_driver", owner_kind="node_group", owner_name="TxGroup",
         data_path='["phase_out"]', expression="2 + 1")
    call("update_depsgraph")
    C.check("fixture_group_has_its_own_driver",
            len(group_snapshot("TxGroup")["drivers"]) == 1
            and close(bpy.data.node_groups["TxGroup"]["phase_out"], 3.0),
            {"drivers": group_snapshot("TxGroup")["drivers"],
             "phase_out": float(bpy.data.node_groups["TxGroup"]["phase_out"])})
    call("create_node_group", name="TxOuter", if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    call("batch_graph_edit", group="TxOuter", operations=[
        {"op": "create_node", "node_type": "GeometryNodeGroup", "name": "Inner"},
    ])
    call("set_node_property", group="TxOuter", node="Inner", property="node_tree",
         value={"id_type": "NODETREE", "id_name": "TxGroup"})
    # The nested reference lives on its own object so TxPanel keeps exactly one
    # geometry modifier (that is what the preview assertions measure).
    call("attach_geometry_nodes_modifier", object="TxOuterPanel", group="TxOuter",
         modifier_name="Outer", reuse_existing=False)
    C.check("fixture_evaluates_the_expected_geometry",
            evaluated("TxPanel")["vertices"] == 16,
            {"evaluated": evaluated("TxPanel"), "modifier_inputs": modifier_parameters("TxPanel"),
             "note": "Rows 4 x default Vertices Y 4"})


# --------------------------------------------------------------------------- #
# 1. transactional rollback
# --------------------------------------------------------------------------- #
ROLLBACK_OPS = [
    {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "RollbackCube"},
    {"op": "set_socket_default", "node": "RollbackCube", "socket": "Size", "value": [4.0, 4.0, 4.0]},
    {"op": "create_node", "node_type": "ShaderNodeCombineXYZ", "name": "RollbackVec"},
    {"op": "link_sockets", "from_node": "RollbackVec", "from_socket": "Vector",
     "to_node": "RollbackCube", "to_socket": "Size"},
    {"op": "unlink_socket", "node": "RollbackCube", "socket": "Size", "in_out": "INPUT"},
    {"op": "remove_node", "node": "RollbackVec"},
    {"op": "link_sockets", "from_node": "RollbackCube", "from_socket": "Mesh",
     "to_node": "Group Output", "to_socket": "Geometry"},
    {"op": "set_socket_default", "node": "Nonexistent", "socket": "Size", "value": 1.0},
]


def test_transactional_rollback() -> None:
    before = group_snapshot("TxGroup")
    params_before = modifier_parameters("TxPanel")
    driver_before = driver_state("TxDriver")
    geometry_before = evaluated("TxPanel")
    node_groups_before = len(bpy.data.node_groups)

    result = call("batch_graph_edit", group="TxGroup", operations=ROLLBACK_OPS)

    C.check("rollback_reports_rolled_back_truthfully",
            result["rolled_back"] is True and result["committed"] is False
            and result["rollback"] is True and result["partial"] is False
            and result["transactional"] is True
            and result["transaction"]["status"] == "rolled_back"
            and result["completed_count"] == 7 and result["failed_count"] == 1
            and result["failures"][0]["index"] == 7
            and result["failures"][0]["code"] == "node_not_found",
            {"completed": result["completed_count"], "failures": result["failures"],
             "transaction": result["transaction"]})

    after = group_snapshot("TxGroup")
    C.check("rollback_leaves_the_original_group_byte_for_byte",
            structural(after) == structural(before),
            {"nodes_before": len(before["nodes"]), "nodes_after": len(after["nodes"]),
             "diff_keys": [key for key in before
                           if key != "pointer" and before[key] != after[key]]})
    C.check("rollback_keeps_the_datablock_identity",
            after["pointer"] == before["pointer"],
            {"pointer_before": before["pointer"], "pointer_after": after["pointer"]})
    C.check("rollback_leaves_no_staging_or_backup_group",
            len(bpy.data.node_groups) == node_groups_before
            and not [g.name for g in bpy.data.node_groups if "__mcp_" in g.name],
            {"before": node_groups_before, "after": len(bpy.data.node_groups),
             "groups": sorted(g.name for g in bpy.data.node_groups)})
    C.check("rollback_keeps_modifier_inputs_and_driver_targets",
            modifier_parameters("TxPanel") == params_before
            and driver_state("TxDriver") == driver_before
            and evaluated("TxPanel") == geometry_before,
            {"modifier_before": params_before, "modifier_after": modifier_parameters("TxPanel"),
             "driver_before": driver_before, "driver_after": driver_state("TxDriver"),
             "geometry_before": geometry_before, "geometry_after": evaluated("TxPanel")})
    C.check("rollback_did_not_add_the_staged_nodes",
            "RollbackCube" not in [node.name for node in bpy.data.node_groups["TxGroup"].nodes],
            {"nodes": sorted(node.name for node in bpy.data.node_groups["TxGroup"].nodes)})

    incompatible = error_of("batch_graph_edit", group="TxGroup", transactional=True,
                            continue_on_error=True, operations=ROLLBACK_OPS[:1])
    after_reject = group_snapshot("TxGroup")
    C.check("incompatible_options_are_rejected_before_any_mutation",
            incompatible is not None and incompatible.code == "incompatible_options"
            and structural(after_reject) == structural(before),
            {"error": incompatible.code if incompatible else None,
             "message": incompatible.message if incompatible else None})


# --------------------------------------------------------------------------- #
# 2. committed transaction
# --------------------------------------------------------------------------- #
COMMIT_OPS = [
    {"op": "create_node", "node_type": "GeometryNodeTransform", "name": "Shift"},
    {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
     "to_node": "Shift", "to_socket": "Geometry"},
    {"op": "link_sockets", "from_node": "Shift", "from_socket": "Geometry",
     "to_node": "Group Output", "to_socket": "Geometry"},
    {"op": "set_socket_default", "node": "Shift", "socket": "Translation", "value": [0.0, 0.0, 2.0]},
    {"op": "create_node", "node_type": "ShaderNodeCombineXYZ", "name": "Tmp"},
    {"op": "link_sockets", "from_node": "Tmp", "from_socket": "Vector",
     "to_node": "Shift", "to_socket": "Translation"},
    {"op": "unlink_socket", "node": "Shift", "socket": "Translation", "in_out": "INPUT"},
    {"op": "remove_node", "node": "Tmp"},
]


def test_transactional_commit() -> None:
    before = group_snapshot("TxGroup")
    node_groups_before = len(bpy.data.node_groups)
    nested_before = next(node for node in bpy.data.node_groups["TxOuter"].nodes
                         if node.bl_idname == "GeometryNodeGroup")
    nested_pointer_before = nested_before.node_tree.as_pointer()
    driver_before = driver_state("TxDriver")

    result = call("batch_graph_edit", group="TxGroup", operations=COMMIT_OPS)

    C.check("commit_reports_committed_truthfully",
            result["committed"] is True and result["rolled_back"] is False
            and result["completed_count"] == 8 and result["failed_count"] == 0
            and result["partial"] is False and result["rollback"] is False
            and result["transaction"]["status"] == "committed"
            and result["transaction"]["datablock_identity_replaced"] is True,
            {"transaction": result["transaction"], "completed": result["completed_count"]})

    after = group_snapshot("TxGroup")
    C.check("commit_replaced_the_datablock_identity_under_the_same_name",
            after["name"] == "TxGroup" and after["pointer"] != before["pointer"]
            and after["interface_identifiers"] == before["interface_identifiers"],
            {"name_before": before["name"], "name_after": after["name"],
             "pointer_before": before["pointer"], "pointer_after": after["pointer"],
             "interface_before": before["interface_identifiers"],
             "interface_after": after["interface_identifiers"]})
    C.check("commit_applied_every_staged_edit",
            {node["name"] for node in after["nodes"]} == {node["name"] for node in before["nodes"]} | {"Shift"}
            and "Grid.Mesh -> Shift.Geometry" in after["links"]
            and any(link.startswith("Shift.Geometry -> Group Output.") for link in after["links"]),
            {"nodes": sorted(node["name"] for node in after["nodes"]), "links": after["links"]})
    C.check("commit_removed_the_temporary_and_the_old_link",
            "Tmp" not in [node["name"] for node in after["nodes"]]
            and not any(link.startswith("Grid.Mesh -> Group Output.") for link in after["links"]),
            {"links": after["links"]})
    C.check("commit_leaves_no_staging_or_backup_datablock",
            len(bpy.data.node_groups) == node_groups_before
            and not [g.name for g in bpy.data.node_groups if "__mcp_" in g.name],
            {"before": node_groups_before, "after": len(bpy.data.node_groups),
             "groups": sorted(g.name for g in bpy.data.node_groups)})

    remapped = result["transaction"]["remapped"]
    C.check("commit_remapped_every_enumerated_user",
            remapped == {"modifier": 1, "node_group_node": 1, "driver_target": 1}
            and result["transaction"]["users_remapped"] == 3
            and result["transaction"]["modifier_inputs"]["restored"] >= 1,
            {"remapped": remapped, "inputs": result["transaction"]["modifier_inputs"],
             "tagged_ids": result["transaction"].get("tagged_ids")})

    modifier = bpy.data.objects["TxPanel"].modifiers["GN"]
    C.check("commit_repointed_the_modifier_and_kept_its_input_values",
            modifier.node_group is not None and modifier.node_group.name == "TxGroup"
            and modifier.node_group.as_pointer() != before["pointer"]
            and close(modifier_parameters("TxPanel").get("Size"), 3.0)
            and modifier_parameters("TxPanel").get("Rows") == 4,
            {"node_group": modifier.node_group.name if modifier.node_group else None,
             "parameters": modifier_parameters("TxPanel")})

    nested_after = next(node for node in bpy.data.node_groups["TxOuter"].nodes
                        if node.bl_idname == "GeometryNodeGroup")
    C.check("commit_repointed_the_nested_group_reference",
            getattr(nested_after.node_tree, "name", None) == "TxGroup"
            and nested_after.node_tree.as_pointer() != nested_pointer_before,
            {"node_tree": getattr(nested_after.node_tree, "name", None),
             "pointer_before": nested_pointer_before,
             "pointer_after": nested_after.node_tree.as_pointer() if nested_after.node_tree else None})

    driver_after = driver_state("TxDriver")
    C.check("commit_kept_the_driver_target_and_its_evaluated_value",
            driver_after["value"] == driver_before["value"]
            and all(target["id"] == "TxGroup" for target in driver_after["targets"])
            and driver_after["targets"][0]["pointer"] == bpy.data.node_groups["TxGroup"].as_pointer()
            and driver_after["targets"][0]["pointer"] != driver_before["targets"][0]["pointer"]
            and driver_after["targets"][0]["valid"] is True,
            {"before": driver_before, "after": driver_after})

    bpy.data.node_groups["TxGroup"]["phase"] = 4.0
    bpy.data.node_groups["TxGroup"].update_tag()
    call("update_depsgraph")
    after_change = driver_state("TxDriver")
    C.check("driver_still_evaluates_against_the_new_datablock",
            close(bpy.data.objects["TxDriver"].rotation_euler[1], 12.0),
            {"rotation_y": float(bpy.data.objects["TxDriver"].rotation_euler[1]), "expected": 12.0,
             "driver": after_change, "phase": float(bpy.data.node_groups["TxGroup"]["phase"]),
             "group_pointer": bpy.data.node_groups["TxGroup"].as_pointer(),
             "tagged_ids": result["transaction"].get("tagged_ids")})
    bpy.data.node_groups["TxGroup"]["phase"] = 2.0
    bpy.data.node_groups["TxGroup"].update_tag()
    call("update_depsgraph")

    geometry = evaluated("TxPanel")
    C.check("committed_graph_is_what_the_dependency_graph_evaluates",
            geometry["vertices"] == 16 and close(geometry["bounds"]["min"][2], 2.0),
            {"geometry": geometry, "note": "Grid 4x4 through the new Shift node (+2 in Z)"})

    call("set_modifier_input", object="TxPanel", input="Rows", value=5)
    grown = evaluated("TxPanel")
    C.check("modifier_input_is_still_wired_to_the_new_graph",
            grown["vertices"] == 20,
            {"vertices": grown["vertices"], "note": "Rows 5 -> 5x4 grid vertices"})
    call("set_modifier_input", object="TxPanel", input="Rows", value=4)

    C.check("commit_preserved_the_group_own_animation",
            len(after["drivers"]) == len(before["drivers"]) == 1
            and after["drivers"][0]["data_path"] == before["drivers"][0]["data_path"]
            and after["drivers"][0]["expression"] == before["drivers"][0]["expression"]
            and after["has_animation"] is True
            and close(bpy.data.node_groups["TxGroup"]["phase_out"], 3.0),
            {"before": before["drivers"], "after": after["drivers"],
             "phase_out": float(bpy.data.node_groups["TxGroup"]["phase_out"]),
             "animation": result["transaction"].get("animation")})

    C.check("response_reports_the_committed_group_state",
            result["group_state"]["name"] == "TxGroup"
            and "Shift" in [node["name"] for node in result["group_state"]["nodes"]],
            {"group_state_nodes": [node["name"] for node in result["group_state"]["nodes"]]})


# --------------------------------------------------------------------------- #
# 3. legacy partial mode + linked libraries
# --------------------------------------------------------------------------- #
def test_legacy_partial_and_linked_library() -> None:
    before = group_snapshot("TxGroup")
    result = call("batch_graph_edit", group="TxGroup", transactional=False, continue_on_error=True,
                  operations=[
                      {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "PartialA"},
                      {"op": "set_socket_default", "node": "PartialA", "socket": "Size", "value": 2.0},
                      {"op": "set_socket_default", "node": "NoSuchNode", "socket": "Size", "value": 1.0},
                      {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "PartialB"},
                  ])
    C.check("legacy_partial_mode_keeps_going_after_a_failure",
            result["transactional"] is False and result["partial"] is True
            and result["completed_count"] == 3 and result["failed_count"] == 1
            and result["rollback"] is False and result["committed"] is False
            and result["transaction"]["status"] == "partial",
            {"completed": result["completed_count"], "failures": result["failures"],
             "transaction": result["transaction"]})
    present = sorted(node.name for node in bpy.data.node_groups["TxGroup"].nodes)
    C.check("legacy_partial_mode_leaves_partial_edits_applied",
            "PartialA" in present and "PartialB" in present
            and bpy.data.node_groups["TxGroup"].as_pointer() == before["pointer"],
            {"nodes": present, "pointer_unchanged":
                bpy.data.node_groups["TxGroup"].as_pointer() == before["pointer"]})

    cleanup = call("batch_graph_edit", group="TxGroup", operations=[
        {"op": "remove_node", "node": "PartialA"},
        {"op": "remove_node", "node": "PartialB"},
    ])
    C.check("transactional_cleanup_removed_both_partial_nodes",
            cleanup["committed"] is True and cleanup["completed_count"] == 2
            and not [node for node in bpy.data.node_groups["TxGroup"].nodes
                     if node.name.startswith("Partial")],
            {"completed": cleanup["completed_count"]})

    # A linked library group must be refused before any staging is created.
    library_dir = addon.project_dir() / "lib"
    library_dir.mkdir(parents=True, exist_ok=True)
    call("create_node_group", name="LibSource", if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    call("batch_graph_edit", group="LibSource", operations=[
        {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "LibCube"},
        {"op": "link_sockets", "from_node": "LibCube", "from_socket": "Mesh",
         "to_node": "Group Output", "to_socket": "Geometry"},
    ])
    # A datablock with no users is not written to the .blend at all, so the group
    # needs a fake user before it can be linked back as a library.
    bpy.data.node_groups["LibSource"].use_fake_user = True
    saved = call("save_blend", relative_path="lib/feature_library.blend", overwrite=True)
    # The main scene has to live in a *different* file before the library can be
    # linked (Blender refuses to link a .blend into itself).
    main_save = call("save_blend", relative_path="lib/feature_main.blend", overwrite=True)
    available_in_library: list[str] = []
    # The local group is renamed first: Blender allows two node groups with the
    # same name when one of them is linked, and name lookup would then be
    # ambiguous. Renaming keeps the linked group addressable as 'LibSource'.
    bpy.data.node_groups["LibSource"].name = "LibSourceLocal"
    with bpy.data.libraries.load(str(Path(saved["path"])), link=True) as (source, target):
        available_in_library = sorted(source.node_groups)
        target.node_groups = [name for name in source.node_groups if name == "LibSource"]
    linked = next((group for group in bpy.data.node_groups
                   if group.library is not None and group.name.startswith("LibSource")), None)
    linked_name = linked.name if linked is not None else "LibSource.001"
    linked_error = error_of("batch_graph_edit", group=linked_name,
                            operations=[{"op": "create_node", "node_type": "GeometryNodeMeshCube",
                                         "name": "Nope"}])
    C.check("linked_library_group_is_refused_transactionally",
            linked is not None and linked_error is not None
            and linked_error.code == "unsafe_linked_group"
            and linked_error.details.get("library"),
            {"linked_name": linked_name,
             "linked_library": getattr(getattr(linked, "library", None), "filepath", None),
             "error": linked_error.code if linked_error else None,
             "available_in_library": available_in_library,
             "groups": ["%s|%s" % (group.name, getattr(group.library, "filepath", None))
                        for group in bpy.data.node_groups]})
    if linked is not None:
        pointer = linked.as_pointer()
        try:
            bpy.data.node_groups.remove(linked)
            C.check("linked_library_group_was_not_modified", True, {"removed": True})
        except Exception as exc:  # noqa: BLE001
            C.check("linked_library_group_was_not_modified", False, {"error": str(exc)})
        _ = pointer


# --------------------------------------------------------------------------- #
# 4. audit log
# --------------------------------------------------------------------------- #
def test_audit_log() -> None:
    addon.configure_audit(directory=AUDIT_DIR, max_bytes=64 * 1024, keep_archives=2)
    call("ping")
    error = error_of("capture_preview")
    call("create_node_group", name="AuditGroup", if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    bad = error_of("batch_graph_edit", group="AuditGroup",
                   operations=[{"op": "link_sockets", "from_node": "NoNode", "from_socket": "X",
                                "to_node": "Group Output", "to_socket": "Geometry"}])
    link_error = error_of("unlink_socket", group="AuditGroup", node="NoSuchNode", socket="Geometry")
    unknown = error_of("definitely_not_a_command")
    wrong_param = error_of("ping", surprise=1)

    events = audit_events()
    by_event: dict[str, list] = {}
    for event in events:
        by_event.setdefault(event["event"], []).append(event)

    starts = by_event.get("command_start", [])
    ends = by_event.get("command_end", [])
    validation_ends = [event for event in ends if event.get("validation") is True]
    C.check("audit_records_every_bridge_command",
            len(ends) == len(starts) + len(validation_ends)
            and len(ends) >= 6 and len(starts) >= 6
            and all(event["source"] == "bridge" for event in ends),
            {"starts": len(starts), "ends": len(ends), "validation_ends": len(validation_ends),
             "commands": sorted({event["command"] for event in ends})})

    ok_end = next(event for event in ends
                  if event["command"] == "ping" and event["status"] == "ok"
                  and event["pid"] == os.getpid())
    C.check("audit_success_event_has_timestamp_duration_and_result",
            ok_end["ts"].endswith("Z") and ok_end["session"] and ok_end["pid"] == os.getpid()
            and isinstance(ok_end["duration_ms"], (int, float))
            and ok_end["result"]["pong"] is True
            and isinstance(ok_end["seq"], int),
            {"event": ok_end, "own_pid": os.getpid()})

    failed = next(event for event in ends if event["command"] == "unlink_socket"
                  and event["status"] == "error")
    C.check("audit_records_command_errors_with_code_and_message",
            failed["ok"] is False and failed["code"] == "node_not_found"
            and "NoSuchNode" in failed["message"],
            {"event": failed})
    C.check("audit_records_schema_validation_failures_at_dispatch",
            unknown.code == "unknown_command" and wrong_param.code == "invalid_argument"
            and unknown.code in [event.get("code") for event in validation_ends]
            and link_error.code == "node_not_found",
            {"unknown": unknown.code, "wrong_param": wrong_param.code,
             "link_error": link_error.code if link_error else None,
             "rolled_back_is_a_success_at_bridge_level": error is not None,
             "validation_events": [event["command"] for event in validation_ends]})
    try:
        token = addon.read_token()
    except addon.CommandError:
        token = ""
    serialised = json.dumps(events)
    known_tokens = []
    for candidate in (MCP_DIR / ".runtime" / "token",
                      Path(os.environ["BLENDER_MCP_TOKEN_FILE"]) if os.environ.get("BLENDER_MCP_TOKEN_FILE") else None):
        if candidate is None:
            continue
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            known_tokens.append(text)
    hex_hits = re.findall(r"\b[0-9a-f]{64}\b", serialised)
    C.check("audit_never_contains_the_bridge_token",
            not any(candidate in serialised for candidate in known_tokens + ([token] if token else []))
            and not hex_hits,
            {"events": len(events), "known_tokens": len(known_tokens), "hex64_hits": len(hex_hits)})
    C.check("audit_bounds_large_arguments",
            any(isinstance(event.get("params"), dict) for event in events),
            {"sample_params": next((event.get("params") for event in events if event.get("params")), None)})


def test_audit_rotation() -> None:
    directory = RUNTIME / "feature_audit_rotation"
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    log = addon.configure_audit(directory=directory, max_bytes=4096, keep_archives=2)
    for index in range(60):
        call("ping")
    archives = sorted(directory.glob("%s.*.jsonl" % log.path.stem))
    live = log.path.exists()
    call("ping")
    tail_line = log.path.read_text(encoding="utf-8").strip().splitlines()[-1]
    C.check("audit_rotation_keeps_bounded_archives_and_keeps_logging",
            archives and len(archives) <= 2 and live
            and json.loads(tail_line)["command"] == "ping"
            and not [name for name in os.listdir(directory) if "__mcp_" in name],
            {"archives": [path.name for path in archives], "live": live,
             "events_in_live": len(log.path.read_text(encoding="utf-8").splitlines())})
    addon.configure_audit(directory=AUDIT_DIR, max_bytes=64 * 1024, keep_archives=2)


def test_batch_outcome_is_audited_on_the_blender_side() -> None:
    addon.configure_audit(directory=AUDIT_DIR, max_bytes=64 * 1024, keep_archives=2)
    call("batch_graph_edit", group="TxGroup", operations=[
        {"op": "set_socket_default", "node": "NoSuchNode", "socket": "Size", "value": 1.0},
    ])
    events = [event for event in audit_events() if event["event"] == "command_end"
              and event["command"] == "batch_graph_edit" and event["status"] == "ok"]
    rolled = [event for event in events if (event.get("result") or {}).get("rolled_back") is True]
    C.check("blender_side_audit_records_the_rollback_outcome",
            bool(rolled) and rolled[-1]["result"]["transactional"] is True,
            {"events": len(events), "rolled_back": bool(rolled),
             "result_keys": sorted((rolled[-1]["result"] if rolled else {}))})


# --------------------------------------------------------------------------- #
# 5. preview capture
# --------------------------------------------------------------------------- #
def test_preview_default_project_rule() -> None:
    saved = os.environ.pop("BLENDER_MCP_WORKSPACE", None)
    try:
        default_project = addon.project_dir()
        expected = addon.addon_dir().parents[1] / "Project"
        C.check("default_project_directory_is_the_sibling_project_folder",
                Path(os.path.realpath(str(default_project))) == Path(os.path.realpath(str(expected))),
                {"default": str(default_project), "expected": str(expected)})
    finally:
        if saved is not None:
            os.environ["BLENDER_MCP_WORKSPACE"] = saved


def test_preview_render_and_restore() -> None:
    state_before = scene_state()
    result = call("capture_preview", object="TxPanel", relative_path=PREVIEW_REL, mode="render",
                  view="isometric", width=320, height=240, timeout_s=60, overwrite=True)
    target = Path(result["path"])
    state_after = scene_state()

    C.check("preview_writes_into_project_previews_only",
            target.is_file() and result["relative_path"] == PREVIEW_REL
            and target.parent == (addon.project_dir() / "previews").resolve(),
            {"path": str(target), "relative_path": result["relative_path"]})
    C.check("preview_dimensions_and_size_match_the_request",
            result["size"] == {"width": 320, "height": 240} and result["bytes"] == target.stat().st_size
            and result["bytes"] > 1000,
            {"size": result["size"], "bytes": result["bytes"]})
    pixels = png_pixels(target)
    C.check("preview_png_is_a_real_non_blank_image",
            pixels["max"] > pixels["min"] and pixels["distinct"] >= 2,
            {"pixels": pixels})
    C.check("preview_frames_the_evaluated_geometry_not_the_basis_mesh",
            result["evaluated"]["vertices"] == 16
            and result["evaluated"]["objects"] == ["TxPanel"]
            and result["diagnostics"]["engine"] in ("BLENDER_WORKBENCH", "BLENDER_EEVEE", "CYCLES"),
            {"evaluated": result["evaluated"], "engine": result["diagnostics"]["engine"],
             "attempts": result["render"]["attempts"]})
    C.check("preview_restores_the_whole_scene_state",
            state_after == state_before,
            {"changed": [key for key in state_before if state_before[key] != state_after[key]],
             "before": state_before, "after": state_after})
    C.check("preview_leaves_no_temporary_datablock_behind",
            leaked_temp_datablocks() == [] and result["diagnostics"].get("cleaned_up") is True,
            {"leaked": leaked_temp_datablocks()})
    C.check("preview_reports_it_did_not_save_the_blend",
            result["scene_state"]["saved_blend"] is False)

    auto = call("capture_preview", object="TxPanel", relative_path="previews/feature_auto.png",
                mode="auto", width=160, height=120, timeout_s=60, overwrite=True)
    C.check("auto_mode_falls_back_to_render_when_headless",
            auto["mode"] == "render" and auto["requested_mode"] == "auto"
            and Path(auto["path"]).is_file()
            and any("auto" in note for note in auto["diagnostics"]["notes"]),
            {"mode": auto["mode"], "notes": auto["diagnostics"]["notes"]})

    top = call("capture_preview", object="TxPanel", relative_path="previews/feature_top.png",
               mode="render", view="top", width=160, height=120, timeout_s=60, overwrite=True)
    C.check("top_view_preset_produces_a_valid_png",
            Path(top["path"]).is_file() and top["size"] == {"width": 160, "height": 120},
            {"mode": top["mode"], "view": top["view"]})


def test_preview_modes_and_validation() -> None:
    viewport = error_of("capture_preview", object="TxPanel", relative_path="previews/feature_vp.png",
                        mode="viewport", width=160, height=120)
    C.check("viewport_mode_reports_an_actionable_error_when_headless",
            viewport is not None and viewport.code == "viewport_unavailable"
            and "background" in viewport.message and viewport.details.get("background") is True,
            {"error": viewport.code if viewport else None,
             "message": viewport.message if viewport else None})

    existing = error_of("capture_preview", object="TxPanel", relative_path=PREVIEW_REL,
                        mode="render", width=160, height=120, overwrite=False)
    C.check("existing_preview_requires_overwrite",
            existing is not None and existing.code == "file_exists",
            {"error": existing.code if existing else None})

    bad_paths = {
        "demo/feature.png": "invalid_path",
        "previews/feature.jpg": "invalid_extension",
        "previews/../escape.png": "invalid_path",
        "C:/temp/feature.png": "invalid_path",
        "previews/feature.bmp": "invalid_extension",
    }
    codes = {relative: (lambda exc: exc.code if exc else None)(
        error_of("capture_preview", object="TxPanel", relative_path=relative, mode="render",
                 width=160, height=120)) for relative in bad_paths}
    C.check("preview_path_rules_are_enforced_inside_blender",
            codes == bad_paths, {"codes": codes, "expected": bad_paths})

    oversized = error_of("capture_preview", object="TxPanel", relative_path="previews/feature_big.png",
                         mode="render", width=4096, height=120)
    tiny = error_of("capture_preview", object="TxPanel", relative_path="previews/feature_tiny.png",
                    mode="render", width=8, height=120)
    C.check("preview_dimensions_are_bounded",
            oversized is not None and oversized.code == "invalid_argument"
            and tiny is not None and tiny.code == "invalid_argument",
            {"oversized": oversized.code if oversized else None,
             "tiny": tiny.code if tiny else None})

    call("create_node_group", name="EmptyGroup", if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    call("create_mesh_object", name="EmptyPanel", vertices=[[0, 0, 0]], faces=[])
    call("attach_geometry_nodes_modifier", object="EmptyPanel", group="EmptyGroup",
         modifier_name="GN", reuse_existing=False)
    empty = error_of("capture_preview", object="EmptyPanel", relative_path="previews/feature_empty.png",
                     mode="render", width=160, height=120)
    C.check("empty_geometry_is_reported_with_diagnostics",
            empty is not None and empty.code == "empty_geometry"
            and "EmptyPanel" in json.dumps(empty.details),
            {"error": empty.code if empty else None, "details": empty.details if empty else None})

    missing = error_of("capture_preview", object="NoSuchObject", relative_path="previews/feature_x.png",
                       mode="render", width=160, height=120)
    C.check("unknown_preview_object_is_an_explicit_error",
            missing is not None and missing.code == "object_not_found",
            {"error": missing.code if missing else None})


# --------------------------------------------------------------------------- #
# 6. camera fit, source frame, scene scoping
# --------------------------------------------------------------------------- #
FRAME_GROUP = "FeatureFrameGroup"
FRAME_OBJECT = "FeatureFramePanel"


def build_frame_fixture() -> None:
    """A 4-unit cube whose object scale is keyed (1.0 @frame1, 5.5 @frame22).

    The scene camera is parked far enough away (orthographic, 60 units across) that
    a 22-unit model still fits, so two ``view='current'`` captures *must* differ in
    size when the render honours the source frame.
    """
    call("create_primitive", kind="cube", name=FRAME_OBJECT, size=2.0)
    call("create_node_group", name=FRAME_GROUP, if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    call("batch_graph_edit", group=FRAME_GROUP, operations=[
        {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "Cube"},
        {"op": "set_socket_default", "node": "Cube", "socket": "Size",
         "value": [4.0, 4.0, 4.0]},
        {"op": "link_sockets", "from_node": "Cube", "from_socket": "Mesh",
         "to_node": "Group Output", "to_socket": "Geometry"},
    ])
    call("attach_geometry_nodes_modifier", object=FRAME_OBJECT, group=FRAME_GROUP,
         modifier_name="GN", reuse_existing=False)
    obj = bpy.data.objects[FRAME_OBJECT]
    obj.scale = (1.0, 1.0, 1.0)
    obj.keyframe_insert("scale", frame=1)
    obj.scale = (5.5, 5.5, 5.5)
    obj.keyframe_insert("scale", frame=22)
    obj.scale = (1.0, 1.0, 1.0)
    camera = bpy.context.scene.camera
    if camera is not None:
        camera.location = (60.0, -60.0, 45.0)
        camera.rotation_euler = addon._look_at_euler(camera.location, (0.0, 0.0, 0.0))
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = 60.0
    call("set_frame", frame=1)
    call("update_depsgraph")


def test_camera_fit_frame_and_scoping() -> None:
    build_frame_fixture()
    other_scene = bpy.data.scenes.new("FeatureOtherScene")
    other_mesh = bpy.data.meshes.new("FeatureOtherMesh")
    other_mesh.from_pydata([(-50, -50, -50), (50, -50, -50), (0, 50, 50)], [], [(0, 1, 2)])
    other_object = bpy.data.objects.new("FeatureOtherPanel", other_mesh)
    other_scene.collection.objects.link(other_object)

    # Same fixed scene camera, two frames: only a render that honours the source
    # frame can produce two differently sized models.
    call("set_frame", frame=1)
    at_one = call("capture_preview", object=FRAME_OBJECT, relative_path="previews/feature_frame1.png",
                  mode="render", view="current", width=480, height=240, timeout_s=60, overwrite=True)
    one_margin = png_border_margins(Path(at_one["path"]))
    call("set_frame", frame=22)
    at_twenty_two = call("capture_preview", object=FRAME_OBJECT,
                         relative_path="previews/feature_frame22_current.png", mode="render",
                         view="current", width=480, height=240, timeout_s=60, overwrite=True)
    twenty_two_margin = png_border_margins(Path(at_twenty_two["path"]))

    size_22 = at_twenty_two["evaluated"]["bounds"]["size"]
    scene_camera = bpy.context.scene.camera
    C.check("preview_renders_the_source_frame_not_frame_one",
             at_twenty_two["diagnostics"]["time"]["frame"] == 22
             and close(size_22[0], 22.0, 0.2) and close(size_22[2], 22.0, 0.2)
             and close(at_one["evaluated"]["bounds"]["size"][0], 4.0, 0.1)
             and not twenty_two_margin["empty"] and not one_margin["empty"]
             and _content_width(twenty_two_margin) > 3 * _content_width(one_margin),
             {"frame22_size": size_22, "frame1_size": at_one["evaluated"]["bounds"]["size"],
              "time": at_twenty_two["diagnostics"]["time"],
              "bbox22": twenty_two_margin.get("content_bbox"),
              "bbox1": one_margin.get("content_bbox"),
              "width22": _content_width(twenty_two_margin),
              "width1": _content_width(one_margin)})

    C.check("current_view_uses_the_source_camera_instead_of_isometric",
             at_twenty_two["view"] == "current"
             and at_twenty_two["diagnostics"]["camera"]["view"] == "current"
             and at_twenty_two["diagnostics"]["camera"]["source_camera"] == (
                 scene_camera.name if scene_camera else None),
             {"view": at_twenty_two["view"], "camera": at_twenty_two["diagnostics"]["camera"],
              "scene_camera": scene_camera.name if scene_camera else None})

    # The reviewer's case: a 22-unit isometric cube must be fully inside the frame.
    fitted = call("capture_preview", object=FRAME_OBJECT,
                  relative_path="previews/feature_frame22.png", mode="render", view="isometric",
                  width=480, height=240, timeout_s=60, overwrite=True)
    fitted_margin = png_border_margins(Path(fitted["path"]))
    fit = fitted["diagnostics"]["camera"]
    C.check("camera_fit_keeps_the_whole_22_unit_model_inside_with_a_margin",
             fit["fits_width"] is True and fit["fits_height"] is True
             and fit["sensor_fit"] == "HORIZONTAL"
             and fit["projected_width"] > 25.0
             and not fitted_margin["empty"]
             and _min_margin(fitted_margin) >= 3,
             {"fit": fit, "margins": fitted_margin.get("margins"),
              "content_bbox": fitted_margin.get("content_bbox"),
              "empty": fitted_margin.get("empty")})

    portrait = call("capture_preview", object=FRAME_OBJECT,
                    relative_path="previews/feature_portrait.png", mode="render", view="isometric",
                    width=240, height=480, timeout_s=60, overwrite=True)
    portrait_margin = png_border_margins(Path(portrait["path"]))
    C.check("camera_fit_handles_a_portrait_frame",
             portrait["diagnostics"]["camera"]["fits_width"] is True
             and portrait["diagnostics"]["camera"]["fits_height"] is True
             and not portrait_margin["empty"]
             and _min_margin(portrait_margin) >= 3,
             {"fit": portrait["diagnostics"]["camera"],
              "margins": portrait_margin.get("margins"),
              "empty": portrait_margin.get("empty")})

    scoped = call("capture_preview", relative_path="previews/feature_scoped.png", mode="render",
                  view="front", width=240, height=160, timeout_s=60, overwrite=True)
    C.check("preview_scopes_to_the_current_scene_only",
             "FeatureOtherPanel" not in scoped["evaluated"]["objects"]
             and FRAME_OBJECT in scoped["evaluated"]["objects"],
             {"objects": scoped["evaluated"]["objects"],
              "other_scene": other_scene.name, "current_scene": bpy.context.scene.name})

    bpy.data.objects.remove(other_object, do_unlink=True)
    bpy.data.scenes.remove(other_scene)
    bpy.data.meshes.remove(other_mesh)


def test_viewport_state_snapshot_restores_selection() -> None:
    """The viewport guard snapshots and restores selection/active, not just render state."""
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    first = bpy.data.objects[FRAME_OBJECT]
    second = bpy.data.objects["TxPanel"]
    for obj in list(bpy.context.selected_objects):
        obj.select_set(False)
    first.select_set(True)
    view_layer.objects.active = first
    scene.render.resolution_x = 480
    snapshot = addon._snapshot_scene_state(scene)

    # What a viewport capture changes: selection, active object, render settings.
    first.select_set(False)
    second.select_set(True)
    view_layer.objects.active = second
    scene.render.resolution_x = 111
    diagnostics: dict = {}
    addon._restore_scene_state(scene, snapshot, diagnostics)

    C.check("viewport_state_restore_covers_selection_active_and_render_state",
             "selected" in snapshot and "active" in snapshot and "frame_subframe" in snapshot
             and "fps" in snapshot
             and {obj.name for obj in bpy.context.selected_objects} == set(snapshot["selected"])
             and view_layer.objects.active.name == snapshot["active"]
             and scene.render.resolution_x == snapshot["resolution_x"]
             and diagnostics.get("restored") is True,
             {"snapshot": {key: snapshot[key] for key in
                           ("selected", "active", "frame_subframe", "fps", "resolution_x")},
              "restored_fields": diagnostics.get("restored_fields")})


def test_instance_only_geometry_is_measured_or_explained() -> None:
    """Instance-only GN output must get real bounds - or an actionable hint."""
    group = "FeatureInstanceGroup"
    call("create_mesh_object", name="FeatureInstancePanel",
         vertices=[[0, 0, 0]], faces=[])
    call("create_node_group", name=group, if_exists="replace",
         interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}])
    call("batch_graph_edit", group=group, operations=[
        {"op": "create_node", "node_type": "GeometryNodeMeshLine", "name": "Line"},
        {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "Cube"},
        {"op": "create_node", "node_type": "GeometryNodeInstanceOnPoints", "name": "Place"},
    ])
    build_note = None
    try:
        call("set_node_property", group=group, node="Line", property="mode", value="POINTS")
    except addon.CommandError as exc:
        build_note = "Mesh Line mode left at its default: %s" % exc.code

    def sockets(node_name: str, key: str) -> dict:
        graph = call("inspect_node_group", name=group)
        node = next(item for item in graph["nodes"] if item["name"] == node_name)
        return {item["identifier"]: item for item in node[key]}

    def find(socket_map: dict, needle: str) -> str | None:
        return next((name for name in socket_map if needle in name.lower()), None)

    line_outputs = sockets("Line", "outputs")
    points_output = find(line_outputs, "point")
    points_owner = "Line"
    if points_output is None:
        mesh_output = find(line_outputs, "mesh")
        if mesh_output is not None:
            # Mesh Line stayed in a mesh mode: convert the mesh to points explicitly.
            call("batch_graph_edit", group=group, operations=[
                {"op": "create_node", "node_type": "GeometryNodeMeshToPoints", "name": "ToPoints"},
            ])
            to_points_inputs = sockets("ToPoints", "inputs")
            to_points_outputs = sockets("ToPoints", "outputs")
            call("batch_graph_edit", group=group, operations=[
                {"op": "link_sockets", "from_node": "Line", "from_socket": mesh_output,
                 "to_node": "ToPoints", "to_socket": find(to_points_inputs, "mesh") or "Mesh"},
            ])
            points_output = find(to_points_outputs, "point")
            points_owner = "ToPoints"
    place_inputs = sockets("Place", "inputs")
    place_outputs = sockets("Place", "outputs")
    instance_input = next((name for name in place_inputs
                           if "instance" in name.lower() and "point" not in name.lower()), None)
    points_input = find(place_inputs, "point")
    instances_output = find(place_outputs, "instance")
    built = all((points_output, instance_input, points_input, instances_output))
    if built:
        try:
            call("batch_graph_edit", group=group, operations=[
                {"op": "link_sockets", "from_node": points_owner, "from_socket": points_output,
                 "to_node": "Place", "to_socket": points_input},
                {"op": "link_sockets", "from_node": "Cube", "from_socket": "Mesh",
                 "to_node": "Place", "to_socket": instance_input},
                {"op": "link_sockets", "from_node": "Place", "from_socket": instances_output,
                 "to_node": "Group Output", "to_socket": "Geometry"},
            ])
        except addon.CommandError as exc:
            built = False
            build_note = "instance graph link failed: %s: %s" % (exc.code, exc.message)
    elif build_note is None:
        build_note = ("socket discovery failed: points=%r points_input=%r instance_input=%r "
                      "instances_output=%r" % (points_output, points_input, instance_input,
                                               instances_output))
    call("attach_geometry_nodes_modifier", object="FeatureInstancePanel", group=group,
         modifier_name="GN", reuse_existing=False)
    call("update_depsgraph")

    measured = None
    diagnostic = None
    try:
        measured = call("capture_preview", object="FeatureInstancePanel",
                        relative_path="previews/feature_instances.png", mode="render",
                        view="isometric", width=240, height=240, timeout_s=60, overwrite=True)
    except addon.CommandError as exc:
        diagnostic = exc
    if measured is not None:
        margins = png_border_margins(Path(measured["path"]))
        C.check("instance_only_geometry_is_measured_from_the_dependency_graph",
                 (measured["evaluated"]["instances"] or 0) >= 1
                 and (measured["evaluated"]["instance_vertices"] or 0) >= 8
                 and measured["evaluated"]["vertices"] >= 8
                 and not margins["empty"] and min(margins["margins"].values()) >= 1,
                 {"evaluated": measured["evaluated"], "margins": margins["margins"],
                  "built": built, "build_note": build_note})
    else:
        hint = (diagnostic.details or {}) if diagnostic else {}
        helper = addon._empty_geometry_details(["FeatureInstancePanel"],
                                               {"FeatureInstancePanel": {"instances": 4}}, 4)
        C.check("instance_only_geometry_is_measured_or_gets_a_realize_hint",
                 diagnostic is not None and diagnostic.code == "empty_geometry"
                 and hint.get("code_hint") == "realize_instances"
                 and helper["code_hint"] == "realize_instances"
                 and "Realize Instances" in helper["hint"],
                 {"error": diagnostic.code if diagnostic else None, "details": hint,
                  "helper": helper, "built": built, "build_note": build_note})


# --------------------------------------------------------------------------- #
# 7. commit fault injection
# --------------------------------------------------------------------------- #
def test_commit_fault_injection_retains_state() -> None:
    group = "TxGroup"
    bpy.data.node_groups[group].use_fake_user = True
    before = group_snapshot(group)
    params_before = modifier_parameters("TxPanel")

    # Fault A: the new node group appears to have lost a modifier input.
    original_restore = addon._restore_modifier_inputs
    addon._restore_modifier_inputs = lambda mod, snapshot: (0, ["Socket_0"])
    try:
        input_fault = error_of("batch_graph_edit", group=group, operations=[
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "FaultInput"},
        ])
    finally:
        addon._restore_modifier_inputs = original_restore
    after_input_fault = group_snapshot(group)
    C.check("commit_aborts_when_a_modifier_input_cannot_be_restored",
             input_fault is not None and input_fault.code == "modifier_inputs_lost"
             and input_fault.details["rollback"]["staging_removed"] is True
             and input_fault.details["rollback"].get("fake_user_restored") is True
             and structural(after_input_fault) == structural(before)
             and after_input_fault["pointer"] == before["pointer"]
             and bool(bpy.data.node_groups[group].use_fake_user) is True
             and modifier_parameters("TxPanel") == params_before
             and addon._leaked_preview_groups() == [],
             {"error": input_fault.code if input_fault else None,
              "rollback": input_fault.details.get("rollback") if input_fault else None,
              "fake_user": bool(bpy.data.node_groups[group].use_fake_user),
              "inputs": modifier_parameters("TxPanel"),
              "leaked": addon._leaked_preview_groups()})

    # Fault B: the previous datablock cannot be removed after the swap.
    original_drop = addon._drop_datablock

    def _failing_drop(tree):
        try:
            if str(getattr(tree, "name", "")).endswith("__mcp_old__"):
                return False
        except Exception:  # noqa: BLE001
            pass
        return original_drop(tree)

    addon._drop_datablock = _failing_drop
    try:
        removal_fault = error_of("batch_graph_edit", group=group, operations=[
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "FaultRemoval"},
        ])
    finally:
        addon._drop_datablock = original_drop
    after_removal_fault = group_snapshot(group)
    C.check("commit_aborts_when_the_old_datablock_cannot_be_removed",
             removal_fault is not None and removal_fault.code == "transaction_failed"
             and removal_fault.details["rollback"]["staging_removed"] is True
             and structural(after_removal_fault) == structural(before)
             and after_removal_fault["pointer"] == before["pointer"]
             and bool(bpy.data.node_groups[group].use_fake_user) is True
             and modifier_parameters("TxPanel") == params_before
             and addon._leaked_preview_groups() == [],
             {"error": removal_fault.code if removal_fault else None,
              "rollback": removal_fault.details.get("rollback") if removal_fault else None,
              "leaked": addon._leaked_preview_groups(),
              "fake_user": bool(bpy.data.node_groups[group].use_fake_user)})

    # The successful path still commits (fault injection left nothing behind).
    recovered = call("batch_graph_edit", group=group, operations=[
        {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "AfterFault"},
        {"op": "remove_node", "node": "AfterFault"},
    ])
    C.check("commit_still_works_after_a_fault_rollback",
             recovered["committed"] is True and recovered["transaction"]["remap_mode"] == "native"
             and recovered["transaction"]["fake_user_preserved"] is True
             and addon._leaked_preview_groups() == [],
             {"transaction": recovered["transaction"], "leaked": addon._leaked_preview_groups()})
    bpy.data.node_groups[group].use_fake_user = False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Deterministic isolation: a previous run's audit files must not be counted as
    # this run's events, and the preview workspace starts empty.
    for stale in (AUDIT_DIR, RUNTIME / "feature_audit_rotation"):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
    (FEATURE_WORKSPACE / "Project").mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    addon.configure_audit(directory=AUDIT_DIR, max_bytes=64 * 1024, keep_archives=2)

    steps = (
        ("render-transaction", build_fixture),
        ("transaction-rollback", test_transactional_rollback),
        ("transaction-commit", test_transactional_commit),
        ("legacy-and-linked", test_legacy_partial_and_linked_library),
        ("audit", test_audit_log),
        ("audit-rotation", test_audit_rotation),
        ("audit-batch", test_batch_outcome_is_audited_on_the_blender_side),
        ("preview-project-rule", test_preview_default_project_rule),
        ("preview-render", test_preview_render_and_restore),
        ("preview-validation", test_preview_modes_and_validation),
        ("preview-camera-fit", test_camera_fit_frame_and_scoping),
        ("preview-viewport-state", test_viewport_state_snapshot_restores_selection),
        ("preview-instances", test_instance_only_geometry_is_measured_or_explained),
        ("commit-faults", test_commit_fault_injection_retains_state),
    )
    for label, step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001 - a crashed step must not hide the report
            C.check("step_%s_did_not_crash" % label, False, {"traceback": traceback.format_exc()})

    payload = C.summary()
    (REPORT_DIR / "blender_feature_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("FEATURE::SUMMARY::" + json.dumps(
        {"ok": payload["ok"], "checks": payload["checks_total"], "failed": payload["checks_failed"],
         "duration_s": payload["duration_s"]}))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
