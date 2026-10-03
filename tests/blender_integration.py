"""Real-Blender integration test for the Blender MCP bridge commands.

Runs INSIDE Blender (it imports ``bpy``) and exercises the same command handlers
the MCP tools call, verifying *evaluated* results rather than trusting that a
property was written:

* Geometry Nodes: parameter changes must change the evaluated vertex count and
  world-space bounds of the modified object.
* Shape keys: influence changes must change the evaluated mesh coordinates.
* Drivers: a custom property must drive a real, evaluated value; a TRANSFORMS
  variable must follow another object; a driver on a node-group socket default
  must change the generated geometry.
* Persistence: the demonstration scene is saved to
  ``<workspace>/Project/demo/parametric_demo.blend`` and re-opened to prove the
  editable node group, the shape keys and the drivers all survive the round trip.
* The "generated geometry ignores base shape keys" case is demonstrated on a
  separate object and asserted, not assumed.

Command::

    blender.exe --background --factory-startup --python-exit-code 1 \
        --python MCP\\tests\\blender_integration.py

Prints ``INTEGRATION::SUMMARY::<json>`` and writes
``MCP/tests/reports/blender_integration_report.json``. Exits non-zero on failure.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import bpy

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
WORKSPACE = MCP_DIR.parent
REPORT_DIR = TESTS_DIR / "reports"
sys.path.insert(0, str(MCP_DIR / "blender"))

import blender_mcp_addon as addon  # noqa: E402  (after sys.path setup)

DEMO_RELATIVE = "demo/parametric_demo.blend"
SCENE_FRAME = 1
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
        print("INTEGRATION::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str)))
        return bool(ok)

    def close(self, value, expected, tol=TOLERANCE) -> bool:
        try:
            return abs(float(value) - float(expected)) <= tol
        except (TypeError, ValueError):
            return False

    @property
    def ok(self) -> bool:
        return all(entry["ok"] for entry in self.checks)

    def summary(self) -> dict:
        failed = [entry["name"] for entry in self.checks if not entry["ok"]]
        return {
            "suite": "blender_integration",
            "blender_version": bpy.app.version_string,
            "python_version": sys.version.split()[0],
            "background": bool(bpy.app.background),
            "factory_startup": bool(bpy.app.factory_startup),
            "duration_s": round(time.time() - self.started, 3),
            "checks_total": len(self.checks),
            "checks_failed": failed,
            "ok": self.ok,
            "checks": self.checks,
        }


C = Checker()


def call(command: str, **params):
    """Call a bridge command directly and surface failures loudly."""
    return addon.dispatch(command, params)


def deps():
    return addon.depsgraph_update()


def evaluated(obj_name: str) -> dict:
    obj = bpy.data.objects[obj_name]
    stats = addon.evaluated_mesh_stats(obj)
    # Objects in this demo only carry a translation, so subtracting it turns the
    # world-space bounds into the mesh-local bounds the assertions talk about.
    origin = obj.matrix_world.translation
    if stats.get("bounds"):
        stats["local_bounds"] = {
            "min": [round(stats["bounds"]["min"][i] - origin[i], 6) for i in range(3)],
            "max": [round(stats["bounds"]["max"][i] - origin[i], 6) for i in range(3)],
            "size": list(stats["bounds"]["size"]),
        }
    return stats


def local(obj_name: str) -> dict:
    return evaluated(obj_name)["local_bounds"]


def action_fcurves(action) -> list:
    """F-Curves of an action across Blender's legacy and slotted action APIs.

    Blender 5.x removed ``Action.fcurves``: animation channels now live in
    ``action.layers[*].strips[*].channelbags[*].fcurves``.
    """
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    curves = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                curves.extend(bag.fcurves)
    return curves


def fail_fast(name: str, exc: Exception):
    C.check(name, False, {"exception": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()})


def socket_identifiers(group_name: str, node_name: str, direction: str = "INPUT") -> dict:
    """name -> identifier for a node's sockets, discovered at runtime."""
    report = call("inspect_node_group", name=group_name)
    for node in report["nodes"]:
        if node["name"] == node_name:
            key = "inputs" if direction.upper().startswith("IN") else "outputs"
            return {entry["name"]: entry["identifier"] for entry in node[key]}
    raise AssertionError("node %r not found in %r" % (node_name, group_name))


def group_input_identifier(group_name: str, socket_name: str) -> str:
    report = call("inspect_node_group", name=group_name, include_sockets=False)
    for item in report["interface"]["inputs"]:
        if item["name"] == socket_name:
            return item["identifier"]
    raise AssertionError("interface input %r not found" % socket_name)


# --------------------------------------------------------------------------- #
def reset_scene() -> None:
    for obj in list(bpy.data.objects):
        if obj.name == "Cube":
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.context.scene.frame_set(SCENE_FRAME)


def build_demo() -> dict:
    """Create the parametric demo objects and their graph/keys/drivers."""
    summary: dict = {}

    # ---- objects -------------------------------------------------------- #
    call(
        "create_mesh_object",
        name="GN_Panel",
        vertices=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]],
        faces=[[0, 1, 2, 3]],
        location=[-4.0, 0.0, 0.0],
    )
    call("create_primitive", kind="uv_sphere", name="Morph_Sphere", radius=1.0, location=[0.0, 0.0, 0.0])
    call("create_primitive", kind="cube", name="Morph_Manual", size=1.0, location=[0.0, -4.0, 0.0])
    call("create_mesh_object", name="GN_Morph", vertices=[
        [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
        [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
    ], faces=[[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]],
        location=[4.0, 0.0, 0.0])
    call("create_primitive", kind="cube", name="Driver_Cube", size=1.0, location=[0.0, 4.0, 0.0])

    # ---- geometry nodes group ------------------------------------------ #
    group = call(
        "create_node_group",
        name="ParametricPanel",
        if_exists="replace",
        interface=[
            {"name": "Size", "socket_type": "NodeSocketFloat", "in_out": "INPUT",
             "default_value": 2.0, "min_value": 0.1, "max_value": 50.0, "subtype": "DISTANCE",
             "description": "Edge length of the generated panel"},
            {"name": "Density", "socket_type": "NodeSocketInt", "in_out": "INPUT",
             "default_value": 4, "min_value": 1, "max_value": 500,
             "description": "Vertices along X"},
            {"name": "Height", "socket_type": "NodeSocketFloat", "in_out": "INPUT",
             "default_value": 1.0, "min_value": -10.0, "max_value": 10.0, "subtype": "DISTANCE",
             "description": "Z offset applied by the Transform node"},
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
        ],
    )
    summary["group_created"] = group["created"]
    C.check(
        "node_group_interface_has_typed_sockets",
        [item["socket_type"] for item in group["group"]["interface"]["inputs"]]
        == ["NodeSocketFloat", "NodeSocketInt", "NodeSocketFloat"],
        {"interface_inputs": group["group"]["interface"]["inputs"]},
    )
    C.check(
        "node_group_has_group_input_output_nodes",
        sorted(node["bl_idname"] for node in group["group"]["nodes"]) == ["NodeGroupInput", "NodeGroupOutput"],
        {"nodes": [node["name"] for node in group["group"]["nodes"]]},
    )

    # Runtime socket discovery: never guess socket names.
    grid_sockets = socket_identifiers("ParametricPanel", "Grid") if False else None
    node_types = call("list_node_types", prefix="GeometryNode", contains="Mesh", limit=50)
    summary["mesh_node_types_sample"] = node_types["types"][:10]
    grid_description = call("describe_node_type", node_type="GeometryNodeMeshGrid")
    summary["grid_socket_names"] = [s["name"] for s in grid_description["inputs"]]
    C.check(
        "node_type_discovery_reports_real_sockets",
        {"Size X", "Size Y", "Vertices X", "Vertices Y"}.issubset(set(summary["grid_socket_names"])),
        {"grid_inputs": grid_description["inputs"]},
    )

    batch = call(
        "batch_graph_edit",
        group="ParametricPanel",
        operations=[
            {"op": "create_node", "node_type": "GeometryNodeMeshGrid", "name": "Grid", "location": [-200, 0]},
            {"op": "create_node", "node_type": "ShaderNodeCombineXYZ", "name": "HeightVec", "location": [0, 0]},
            {"op": "create_node", "node_type": "GeometryNodeTransform", "name": "XForm", "location": [200, 0]},
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "XForm", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "XForm", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
             "to_node": "Grid", "to_socket": "Size X"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Size",
             "to_node": "Grid", "to_socket": "Size Y"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Density",
             "to_node": "Grid", "to_socket": "Vertices X"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Height",
             "to_node": "HeightVec", "to_socket": "Z"},
            {"op": "link_sockets", "from_node": "HeightVec", "from_socket": "Vector",
             "to_node": "XForm", "to_socket": "Translation"},
            {"op": "set_socket_default", "node": "Grid", "socket": "Vertices Y", "value": 4},
        ],
    )
    summary["batch_ops_completed"] = batch["completed_count"]
    C.check(
        "batch_graph_edit_completed_all_operations",
        batch["completed_count"] == batch["total"] if "total" in batch else batch["failed_count"] == 0,
        {"completed": batch["completed_count"], "failures": batch["failures"], "partial": batch["partial"]},
    )
    C.check("batch_graph_edit_reports_no_rollback", batch["rollback"] is False, batch["note"])
    C.check(
        "batch_graph_edit_reports_a_committed_transaction",
        batch["transactional"] is True and batch["committed"] is True
        and batch["rolled_back"] is False
        and batch["transaction"]["status"] == "committed",
        {"transactional": batch["transactional"], "transaction": batch["transaction"]},
    )

    # Negative: an impossible socket link must be an error, not a silent success.
    link_error = None
    try:
        call("link_sockets", group="ParametricPanel", from_node="Grid", from_socket="Mesh",
             to_node="XForm", to_socket="Translation")
    except addon.CommandError as exc:
        link_error = exc
    C.check(
        "impossible_link_is_rejected",
        link_error is not None and link_error.code == "incompatible_sockets",
        {"error": link_error.message if link_error else None},
    )

    attach = call(
        "attach_geometry_nodes_modifier",
        object="GN_Panel",
        group="ParametricPanel",
        modifier_name="ParametricPanel",
        parameters={"Size": 2.0, "Density": 4, "Height": 1.0},
    )
    summary["modifier_parameters"] = attach["parameters"]
    C.check(
        "modifier_exposes_named_parameters",
        attach["parameters_failed"] == [] and len(attach["parameters"]) >= 3,
        {"parameters": attach["parameters"], "failed": attach["parameters_failed"]},
    )

    # A third morph object whose key is NOT driven, for manual influence tests.
    manual = bpy.data.objects["Morph_Manual"]
    call("create_shape_key", object="Morph_Manual", name="Taper", value=0.0, slider_min=0.0, slider_max=1.0)
    call(
        "set_shape_key_coordinates",
        object="Morph_Manual",
        key_name="Taper",
        deltas=[[2.0, 0.0, 0.0]] * len(manual.data.vertices),
    )

    # ---- shape keys ----------------------------------------------------- #
    sphere = bpy.data.objects["Morph_Sphere"]
    radius = max(v.co.x for v in sphere.data.vertices)
    keys = call("create_shape_key", object="Morph_Sphere", name="ScaleX", value=0.0, slider_min=0.0, slider_max=1.0)
    C.check("shape_key_basis_created_automatically", keys["basis_created"] and "Basis" in [k["name"] for k in keys["keys"]],
            {"keys": keys["keys"]})
    count = keys["mesh_vertices"]
    deltas = [[0.5 * sphere.data.vertices[i].co.x, 0.0, 0.0] for i in range(count)]
    written = call("set_shape_key_coordinates", object="Morph_Sphere", key_name="ScaleX", deltas=deltas)
    C.check(
        "shape_key_deltas_written_for_every_vertex",
        written["changed"] == count and written["key_vertices"] == count,
        {"changed": written["changed"], "vertices": count, "radius": radius},
    )

    # Negative: wrong coordinate count must be rejected with the real topology error.
    topology_error = None
    try:
        call("set_shape_key_coordinates", object="Morph_Sphere", key_name="ScaleX", coordinates=[[0.0, 0.0, 0.0]])
    except addon.CommandError as exc:
        topology_error = exc
    C.check(
        "shape_key_topology_mismatch_is_rejected",
        topology_error is not None and topology_error.code == "topology_mismatch",
        {"error": topology_error.message if topology_error else None,
         "details": topology_error.details if topology_error else None},
    )

    basis_error = None
    try:
        call("set_shape_key_coordinates", object="Morph_Sphere", key_name="Basis", coordinates=[[0.0, 0.0, 0.0]] * count)
    except addon.CommandError as exc:
        basis_error = exc
    C.check(
        "shape_key_basis_is_write_protected",
        basis_error is not None and basis_error.code == "basis_protected",
        {"error": basis_error.message if basis_error else None},
    )

    # A malformed entry in the middle of the payload must not partially write:
    # the whole payload is validated before the first coordinate is stored.
    before_bad = call("inspect_shape_keys", object="Morph_Sphere")
    sample_before = [
        list(bpy.data.objects["Morph_Sphere"].data.shape_keys.key_blocks["ScaleX"].data[i].co)
        for i in (0, 1, 2)
    ]
    partial_error = None
    malformed_full_length = [[50.0, 0.0, 0.0] for _ in range(count)]
    malformed_full_length[1] = "not-a-vector"
    try:
        call(
            "set_shape_key_coordinates",
            object="Morph_Sphere",
            key_name="ScaleX",
            coordinates=malformed_full_length,
        )
    except addon.CommandError as exc:
        partial_error = exc
    sample_after = [
        list(bpy.data.objects["Morph_Sphere"].data.shape_keys.key_blocks["ScaleX"].data[i].co)
        for i in (0, 1, 2)
    ]
    C.check(
        "shape_key_rejects_malformed_payload_without_partial_write",
        partial_error is not None
        and partial_error.code in ("invalid_argument", "topology_mismatch")
        and sample_before == sample_after,
        {"error": partial_error.message if partial_error else None,
         "before": sample_before, "after": sample_after,
         "key_count": len(before_bad["keys"])},
    )

    # A second object: a shape key AND a generated-geometry modifier together.
    gn_morph = bpy.data.objects["GN_Morph"]
    call("create_shape_key", object="GN_Morph", name="Stretch", value=0.0)
    call(
        "set_shape_key_coordinates",
        object="GN_Morph",
        key_name="Stretch",
        deltas=[[1.0, 0.0, 0.0]] * len(gn_morph.data.vertices),
    )
    call("attach_geometry_nodes_modifier", object="GN_Morph", group="ParametricPanel",
         modifier_name="ParametricPanel", parameters={"Size": 2.0, "Density": 4, "Height": 1.0})

    # ---- drivers -------------------------------------------------------- #
    # Custom properties first: driver targets are validated with path_resolve,
    # so an input property must exist before a variable can point at it.
    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=0.0)
    call("set_custom_property", owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=0.0)
    call("set_custom_property", owner_kind="scene", key="rows", value=4)

    # Missing-input negative: a driver variable pointed at a property that does
    # not exist must be refused before the driver is created.
    missing_prop_error = None
    try:
        call("add_driver", owner_kind="object", owner_name="Driver_Cube", data_path="scale", index=0,
             expression="1 + nope", variables=[{
                 "name": "nope", "type": "SINGLE_PROP",
                 "targets": [{"id_type": "OBJECT", "id_name": "Driver_Cube", "data_path": '["nope"]'}],
             }])
    except addon.CommandError as exc:
        missing_prop_error = exc
    C.check(
        "driver_target_on_missing_property_is_rejected",
        missing_prop_error is not None and missing_prop_error.code == "invalid_data_path",
        {"error": missing_prop_error.message if missing_prop_error else None},
    )

    # Unrelated animation that must survive every driver edit.
    driver_cube = bpy.data.objects["Driver_Cube"]
    driver_cube.location[0] = 0.0
    driver_cube.keyframe_insert(data_path="location", index=0, frame=SCENE_FRAME)
    unrelated_before = len(action_fcurves(driver_cube.animation_data.action))

    rotation = call(
        "add_driver",
        owner_kind="object",
        owner_name="Driver_Cube",
        data_path="rotation_euler",
        index=2,
        expression="phase * 2",
        variables=[{
            "name": "phase",
            "type": "SINGLE_PROP",
            "targets": [{"id_type": "OBJECT", "id_name": "Driver_Cube", "data_path": '["phase"]'}],
        }],
    )
    height = call(
        "add_driver",
        owner_kind="object",
        owner_name="Driver_Cube",
        data_path="location",
        index=2,
        expression="height",
        variables=[{
            "name": "height",
            "type": "TRANSFORMS",
            "targets": [{"id_type": "OBJECT", "id_name": "Morph_Sphere",
                         "transform_type": "LOC_Z", "transform_space": "WORLD_SPACE"}],
        }],
    )
    rows_driver = call(
        "add_driver",
        owner_kind="node_group",
        owner_name="ParametricPanel",
        data_path='nodes["Grid"].inputs["Vertices Y"].default_value',
        expression="rows",
        variables=[{
            "name": "rows",
            "type": "SINGLE_PROP",
            "targets": [{"id_type": "SCENE", "id_name": bpy.context.scene.name, "data_path": '["rows"]'}],
        }],
    )
    key_driver = call(
        "add_driver",
        owner_kind="shape_keys",
        owner_name="Morph_Sphere",
        data_path='key_blocks["ScaleX"].value',
        expression="bulge",
        variables=[{
            "name": "bulge",
            "type": "SINGLE_PROP",
            "targets": [{"id_type": "OBJECT", "id_name": "Morph_Sphere", "data_path": '["bulge"]'}],
        }],
    )
    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=0.0)
    call("update_depsgraph")

    summary["drivers"] = {
        "rotation_valid": rotation["channels"][0]["is_valid"],
        "height_valid": height["channels"][0]["is_valid"],
        "rows_valid": rows_driver["channels"][0]["is_valid"],
        "key_valid": key_driver["channels"][0]["is_valid"],
    }
    C.check(
        "drivers_are_valid",
        all(summary["drivers"].values()),
        summary["drivers"],
    )
    C.check(
        "unrelated_animation_preserved",
        len(action_fcurves(driver_cube.animation_data.action)) == unrelated_before
        and any(fc.data_path == "location" and fc.array_index == 0
                for fc in action_fcurves(driver_cube.animation_data.action)),
        {"fcurves_before": unrelated_before,
         "fcurves_after": [fc.data_path for fc in action_fcurves(driver_cube.animation_data.action)]},
    )

    # Negative: a data path that does not resolve must be rejected.
    path_error = None
    try:
        call("add_driver", owner_kind="object", owner_name="Driver_Cube",
             data_path="definitely_not_a_property", index=0, expression="1.0")
    except addon.CommandError as exc:
        path_error = exc
    C.check(
        "invalid_driver_data_path_is_rejected",
        path_error is not None and path_error.code == "invalid_data_path",
        {"error": path_error.message if path_error else None},
    )

    # Negative: an unknown variable name in the expression must be rejected.
    expression_error = None
    try:
        call("add_driver", owner_kind="object", owner_name="Driver_Cube",
             data_path="scale", index=0, expression="missing_variable * 2")
    except addon.CommandError as exc:
        expression_error = exc
    C.check(
        "unknown_driver_variable_is_rejected",
        expression_error is not None and expression_error.code == "unknown_variable",
        {"error": expression_error.message if expression_error else None},
    )

    # Negative: no Python execution through the expression channel.
    exec_error = None
    try:
        call("add_driver", owner_kind="object", owner_name="Driver_Cube",
             data_path="scale", index=0, expression="__import__('os').system('echo hi')")
    except addon.CommandError as exc:
        exec_error = exc
    C.check(
        "driver_expression_rejects_python_exec",
        exec_error is not None and exec_error.code == "invalid_expression",
        {"error": exec_error.message if exec_error else None},
    )

    return summary


def assert_evaluation(summary: dict) -> None:
    """The heart of the test: evaluated results, not property reads."""
    # --- geometry nodes parameters change evaluated geometry ------------- #
    before = evaluated("GN_Panel")
    summary["gn_baseline"] = before
    C.check(
        "gn_baseline_evaluated_geometry",
        before["evaluated"]["vertices"] == 16
        and C.close(before["local_bounds"]["max"][0], 1.0)
        and C.close(before["local_bounds"]["max"][2], 1.0),
        before,
    )

    call("set_modifier_input", object="GN_Panel", input="Size", value=6.0)
    after_size = local("GN_Panel")
    C.check(
        "gn_parameter_size_changes_evaluated_bounds",
        C.close(after_size["max"][0], 3.0) and C.close(after_size["min"][0], -3.0),
        after_size,
    )

    call("set_modifier_input", object="GN_Panel", input="Height", value=2.5)
    after_height = local("GN_Panel")
    C.check(
        "gn_parameter_height_changes_evaluated_bounds",
        C.close(after_height["max"][2], 2.5) and C.close(after_height["min"][2], 2.5),
        after_height,
    )

    density = call("set_modifier_input", object="GN_Panel", input="Density", value=6, include_evaluated=True)
    C.check(
        "gn_parameter_density_changes_evaluated_vertex_count",
        density["evaluated"]["evaluated"]["vertices"] == 24,
        density["evaluated"]["evaluated"],
    )

    # Parameter readback must match what we set (Blender-side truth).
    readback = density["parameters"]
    by_name = {entry["name"]: entry["value"] for entry in readback.values()}
    C.check(
        "modifier_parameter_readback_matches",
        by_name.get("Size") == 6.0 and by_name.get("Density") == 6 and by_name.get("Height") == 2.5,
        by_name,
    )

    # Unlinked socket driven from a SCENE custom property on frame change.
    call("set_custom_property", owner_kind="scene", key="rows", value=2)
    bpy.context.scene.frame_set(SCENE_FRAME + 1)
    after_rows = evaluated("GN_Panel")
    C.check(
        "node_group_driver_from_scene_property_changes_geometry",
        after_rows["evaluated"]["vertices"] == 12,
        {"vertices": after_rows["evaluated"]["vertices"], "scene_rows": bpy.context.scene["rows"]},
    )
    call("set_custom_property", owner_kind="scene", key="rows", value=4)
    bpy.context.scene.frame_set(SCENE_FRAME + 2)
    restored = evaluated("GN_Panel")
    C.check(
        "node_group_driver_reverses_cleanly",
        restored["evaluated"]["vertices"] == 24,
        {"vertices": restored["evaluated"]["vertices"]},
    )

    # --- shape key influence changes evaluated geometry ------------------ #
    # Manual (non-driven) influence on Morph_Manual: the key shifts every vertex
    # by +2 in X, so the evaluated cube grows from [-0.5, 0.5] to [-0.5, 2.5].
    cube = bpy.data.objects["Morph_Manual"]
    cube_half = max(v.co.x for v in cube.data.vertices)
    manual_base = local("Morph_Manual")
    influence_0 = call("set_shape_key", object="Morph_Manual", key_name="Taper", value=0.0)
    at_zero = local("Morph_Manual")
    influence_1 = call("set_shape_key", object="Morph_Manual", key_name="Taper", value=1.0)
    at_one = local("Morph_Manual")
    C.check(
        "shape_key_influence_changes_evaluated_mesh",
        C.close(at_zero["max"][0], cube_half)
        and C.close(at_one["max"][0], cube_half + 2.0)
        and not influence_1["driven"],
        {
            "base_max_x": manual_base["max"][0],
            "value_0_max_x": at_zero["max"][0],
            "value_1_max_x": at_one["max"][0],
            "expected_value_1": cube_half + 2.0,
            "readback_value": influence_1["effective_value"],
        },
    )
    call("set_shape_key", object="Morph_Manual", key_name="Taper", value=0.5)
    at_half = local("Morph_Manual")
    C.check(
        "shape_key_influence_is_proportional",
        C.close(at_half["max"][0], cube_half + 1.0) and C.close(at_half["min"][0], -cube_half + 1.0),
        {"value": 0.5, "max_x": at_half["max"][0], "min_x": at_half["min"][0],
         "expected_max": cube_half + 1.0, "expected_min": -cube_half + 1.0,
         "note": "every vertex is offset by the same +X delta, so the cube translates by value*2"},
    )
    call("set_shape_key", object="Morph_Manual", key_name="Taper", value=0.0)
    back = local("Morph_Manual")
    C.check(
        "shape_key_influence_returns_to_basis",
        C.close(back["max"][0], manual_base["max"][0]),
        {"max_x": back["max"][0], "baseline": manual_base["max"][0]},
    )

    # --- the driven key: influence follows its custom property ----------- #
    sphere = bpy.data.objects["Morph_Sphere"]
    radius = max(v.co.x for v in sphere.data.vertices)
    summary["sphere_radius"] = radius
    base = evaluated("Morph_Sphere")
    summary["shape_key_base"] = base
    driven_attempt = call("set_shape_key", object="Morph_Sphere", key_name="ScaleX", value=1.0)
    C.check(
        "driven_shape_key_reports_that_it_is_driven",
        driven_attempt["driven"] is True
        and driven_attempt["driver"]["data_path"] == 'key_blocks["ScaleX"].value'
        and driven_attempt["warnings"],
        {"driven": driven_attempt["driven"], "driver": driven_attempt["driver"],
         "warnings": driven_attempt["warnings"]},
    )
    call("set_custom_property", owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=1.0)
    bpy.context.scene.frame_set(SCENE_FRAME + 3)
    driven = local("Morph_Sphere")
    key_value = bpy.data.objects["Morph_Sphere"].data.shape_keys.key_blocks["ScaleX"].value
    C.check(
        "shape_key_driver_from_custom_property",
        C.close(key_value, 1.0)
        and C.close(driven["max"][0], 1.5 * radius)
        and C.close(driven["min"][0], -1.5 * radius),
        {"key_value": key_value, "radius": radius, "min_x": driven["min"][0], "max_x": driven["max"][0],
         "expected": 1.5 * radius, "baseline_max_x": base["local_bounds"]["max"][0]},
    )
    call("set_custom_property", owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=0.5)
    bpy.context.scene.frame_set(SCENE_FRAME + 4)
    half_driven = local("Morph_Sphere")
    C.check(
        "driven_shape_key_influence_is_proportional",
        C.close(half_driven["max"][0], 1.25 * radius),
        {"bulge": 0.5, "max_x": half_driven["max"][0], "expected": 1.25 * radius},
    )
    call("set_custom_property", owner_kind="object", owner_name="Morph_Sphere", key="bulge", value=0.0)
    bpy.context.scene.frame_set(SCENE_FRAME + 5)
    restored_key = local("Morph_Sphere")
    C.check(
        "driven_shape_key_returns_to_basis",
        C.close(restored_key["max"][0], radius),
        {"max_x": restored_key["max"][0], "expected": radius},
    )

    # --- drivers driven by custom properties / transforms ---------------- #
    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=0.75)
    call("update_depsgraph")
    rotation_z = bpy.data.objects["Driver_Cube"].rotation_euler[2]
    C.check(
        "object_driver_from_custom_property_evaluates",
        C.close(rotation_z, 1.5),
        {"phase": 0.75, "rotation_euler_z": rotation_z, "expected": 1.5},
    )

    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=-1.0)
    call("update_depsgraph")
    rotation_z = bpy.data.objects["Driver_Cube"].rotation_euler[2]
    C.check(
        "object_driver_tracks_custom_property_changes",
        C.close(rotation_z, -2.0),
        {"phase": -1.0, "rotation_euler_z": rotation_z, "expected": -2.0},
    )

    bpy.data.objects["Morph_Sphere"].location[2] = 2.0
    call("update_depsgraph")
    cube_z = bpy.data.objects["Driver_Cube"].location[2]
    C.check(
        "transforms_driver_follows_other_object",
        C.close(cube_z, 2.0),
        {"morph_z": 2.0, "cube_z": cube_z},
    )

    inspected = call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")
    rotation_entry = next(d for d in inspected["drivers"] if d["data_path"] == "rotation_euler")
    height_entry = next(d for d in inspected["drivers"] if d["data_path"] == "location")
    C.check(
        "inspect_drivers_reports_variables_and_targets",
        rotation_entry["variables"][0]["targets"][0]["data_path"] == '["phase"]'
        and height_entry["variables"][0]["type"] == "TRANSFORMS"
        and height_entry["variables"][0]["targets"][0]["id_name"] == "Morph_Sphere",
        {"rotation": rotation_entry["variables"], "height": height_entry["variables"]},
    )

    # --- the generated-geometry vs base-shape-key case ------------------- #
    gn_morph = bpy.data.objects["GN_Morph"]
    gn_before = evaluated("GN_Morph")
    call("set_shape_key", object="GN_Morph", key_name="Stretch", value=1.0)
    gn_after = evaluated("GN_Morph")
    key_value = gn_morph.data.shape_keys.key_blocks["Stretch"].value
    summary["gn_morph_shape_key_value"] = key_value
    C.check(
        "shape_key_on_gn_object_is_ignored_by_generated_geometry",
        C.close(key_value, 1.0)
        and gn_after["evaluated"]["vertices"] == gn_before["evaluated"]["vertices"]
        and gn_after["bounds"] == gn_before["bounds"],
        {
            "key_value": key_value,
            "explanation": (
                "GN_Morph keeps its 8-vertex cube as the shape key domain, but the Geometry Nodes "
                "modifier replaces the mesh with a generated grid, so the evaluated bounds and vertex "
                "count come from the grid and the key influence is invisible. Morphing and generated "
                "geometry must live on separate objects to be visible at the same time."
            ),
            "vertices_before": gn_before["evaluated"]["vertices"],
            "vertices_after": gn_after["evaluated"]["vertices"],
            "bounds_before": gn_before["bounds"],
            "bounds_after": gn_after["bounds"],
        },
    )
    C.check(
        "gn_object_base_mesh_still_has_the_key",
        len(gn_morph.data.shape_keys.key_blocks) == 2 and len(gn_morph.data.shape_keys.key_blocks["Stretch"].data)
        == len(gn_morph.data.vertices),
        {"keys": [k.name for k in gn_morph.data.shape_keys.key_blocks]},
    )

    # Removing a driver must actually remove it and keep the rest intact.
    removed = call("remove_driver", owner_kind="object", owner_name="Driver_Cube",
                   data_path="rotation_euler", index=2)
    remaining = call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")
    C.check(
        "remove_driver_removes_only_the_requested_driver",
        removed["removed"] and len(remaining["drivers"]) == 1 and remaining["drivers"][0]["data_path"] == "location",
        {"remaining": [d["data_path"] for d in remaining["drivers"]]},
    )
    # Restore it for the saved demo file.
    call("add_driver", owner_kind="object", owner_name="Driver_Cube", data_path="rotation_euler", index=2,
         expression="phase * 2",
         variables=[{"name": "phase", "type": "SINGLE_PROP",
                     "targets": [{"id_type": "OBJECT", "id_name": "Driver_Cube", "data_path": '["phase"]'}]}])
    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=0.75)
    call("update_depsgraph")
    C.check(
        "driver_re_added_after_removal",
        C.close(bpy.data.objects["Driver_Cube"].rotation_euler[2], 1.5),
        {"rotation_euler_z": bpy.data.objects["Driver_Cube"].rotation_euler[2]},
    )

    # A semantically broken update must restore the previous expression/variables.
    driver_before = next(
        d for d in call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")["drivers"]
        if d["data_path"] == "rotation_euler"
    )
    update_error = None
    try:
        call("update_driver", owner_kind="object", owner_name="Driver_Cube", data_path="rotation_euler",
             index=2, expression="1 / (phase - phase)")
    except addon.CommandError as exc:
        update_error = exc
    driver_after = next(
        d for d in call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")["drivers"]
        if d["data_path"] == "rotation_euler"
    )
    call("update_depsgraph")
    C.check(
        "failed_driver_update_restores_previous_expression",
        update_error is not None
        and update_error.code == "invalid_expression"
        and driver_after["expression"] == driver_before["expression"]
        and driver_after["is_valid"] is True
        and [v["name"] for v in driver_after["variables"]] == [v["name"] for v in driver_before["variables"]]
        and C.close(bpy.data.objects["Driver_Cube"].rotation_euler[2], 1.5),
        {"error": update_error.message if update_error else None,
         "expression_before": driver_before["expression"],
         "expression_after": driver_after["expression"],
         "is_valid_after": driver_after["is_valid"],
         "rotation_euler_z": bpy.data.objects["Driver_Cube"].rotation_euler[2]},
    )


def persist_and_reload(summary: dict) -> None:
    """Save the demo file, re-open it and prove the edits are still live."""
    # Make the run repeatable: the test owns Project/demo and removes its previous
    # artefacts so the "no overwrite" negative check below is meaningful.
    demo_path = addon.resolve_project_path(DEMO_RELATIVE)
    for stale in (Path(str(demo_path)), Path(str(demo_path) + "1"), Path(str(demo_path) + "2")):
        if stale.exists():
            stale.unlink()
            print("INTEGRATION::INFO::removed stale %s" % stale)

    # Negative: overwrite must be opt-in. Write once, then refuse a silent overwrite.
    first = call("save_blend", relative_path=DEMO_RELATIVE, overwrite=False)
    summary["saved_path"] = first["path"]
    summary["saved_bytes"] = first["bytes"]
    exists_error = None
    try:
        call("save_blend", relative_path=DEMO_RELATIVE, overwrite=False)
    except addon.CommandError as exc:
        exists_error = exc
    C.check(
        "save_blend_refuses_overwrite_by_default",
        exists_error is not None and exists_error.code == "file_exists",
        {"error": exists_error.message if exists_error else None, "path": first["path"]},
    )
    traversal_error = None
    try:
        call("save_blend", relative_path="../outside_project.blend", overwrite=True)
    except addon.CommandError as exc:
        traversal_error = exc
    C.check(
        "save_blend_rejects_path_traversal",
        traversal_error is not None and traversal_error.code == "invalid_path",
        {"error": traversal_error.message if traversal_error else None},
    )
    absolute_error = None
    try:
        call("save_blend", relative_path=str(MCP_DIR / "outside.blend"), overwrite=True)
    except addon.CommandError as exc:
        absolute_error = exc
    C.check(
        "save_blend_rejects_absolute_paths",
        absolute_error is not None and absolute_error.code == "invalid_path",
        {"error": absolute_error.message if absolute_error else None},
    )
    # Re-save explicitly so this run's content is what is on disk.
    saved = call("save_blend", relative_path=DEMO_RELATIVE, overwrite=True)
    summary["saved_bytes"] = saved["bytes"]
    summary["saved_overwritten"] = saved["overwritten"]
    C.check("save_blend_overwrite_true_writes_file", saved["saved"] and saved["bytes"] > 1000, saved)

    before = {
        "nodes": [n["name"] for n in call("inspect_node_group", name="ParametricPanel", include_sockets=False)["nodes"]],
        "links": len(call("inspect_node_group", name="ParametricPanel", include_sockets=False)["links"]),
        "shape_keys": [k["name"] for k in call("inspect_shape_keys", object="Morph_Sphere")["keys"]],
        "drivers_rotation": len(call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")["drivers"]),
        "drivers_group": len(call("inspect_drivers", owner_kind="node_group", owner_name="ParametricPanel")["drivers"]),
        "drivers_keys": len(call("inspect_drivers", owner_kind="shape_keys", owner_name="Morph_Sphere")["drivers"]),
    }

    path = Path(first["path"])
    bpy.ops.wm.open_mainfile(filepath=str(path))
    after = {
        "nodes": [n["name"] for n in call("inspect_node_group", name="ParametricPanel", include_sockets=False)["nodes"]],
        "links": len(call("inspect_node_group", name="ParametricPanel", include_sockets=False)["links"]),
        "shape_keys": [k["name"] for k in call("inspect_shape_keys", object="Morph_Sphere")["keys"]],
        "drivers_rotation": len(call("inspect_drivers", owner_kind="object", owner_name="Driver_Cube")["drivers"]),
        "drivers_group": len(call("inspect_drivers", owner_kind="node_group", owner_name="ParametricPanel")["drivers"]),
        "drivers_keys": len(call("inspect_drivers", owner_kind="shape_keys", owner_name="Morph_Sphere")["drivers"]),
    }
    summary["persistence_before"] = before
    summary["persistence_after"] = after
    C.check("saved_file_reopens_with_same_graph", before == after, {"before": before, "after": after})

    # The reloaded group must still be editable and still affect geometry.
    call("set_modifier_input", object="GN_Panel", input="Density", value=5)
    reloaded = evaluated("GN_Panel")
    C.check(
        "reloaded_group_still_parametric",
        reloaded["evaluated"]["vertices"] == 5 * 4,
        {"vertices": reloaded["evaluated"]["vertices"], "expected": 20},
    )
    # And the reloaded drivers must still evaluate.
    call("set_custom_property", owner_kind="object", owner_name="Driver_Cube", key="phase", value=3.0)
    call("update_depsgraph")
    C.check(
        "reloaded_driver_still_evaluates",
        C.close(bpy.data.objects["Driver_Cube"].rotation_euler[2], 6.0),
        {"rotation_euler_z": bpy.data.objects["Driver_Cube"].rotation_euler[2], "expected": 6.0},
    )


def main() -> int:
    try:
        reset_scene()
        summary = build_demo()
        assert_evaluation(summary)
        persist_and_reload(summary)
    except Exception as exc:  # noqa: BLE001 - report everything
        fail_fast("integration_run_completed", exc)
        summary = {}

    payload = C.summary()
    payload.update({k: v for k, v in (summary or {}).items() if k not in payload})
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "blender_integration_report.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("INTEGRATION::SUMMARY::" + json.dumps(payload, default=str))
    return 0 if C.ok else 1


if __name__ == "__main__":
    code = main()
    print("INTEGRATION::EXIT::%d" % code)
    if code != 0:
        raise SystemExit(code)
