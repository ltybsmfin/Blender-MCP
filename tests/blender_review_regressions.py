"""Review-delta regressions for the Blender MCP bridge (real Blender required).

Run::

    Version\\5_2_1_LTS\\blender.exe --background --factory-startup --python-exit-code 1 \
        --python MCP\\tests\\blender_review_regressions.py

Covers the review items only; everything is built in memory (no .blend is saved,
no GUI is started) and the report is written to
``MCP/tests/reports/review_report.json``.

1. Datablock references: GeometryNodeGroup.node_tree (including clearing), the
   Object Info "Object" socket, group-interface pointer defaults and exposed
   Geometry-Nodes modifier pointer parameters; wrong/missing types are refused
   without touching the previous pointer; readback serialises references.
2. Nested group reuse through the tools, with evaluated geometry.
3. Non-finite shape key coordinates are refused before any vertex is written.
4. Driver variable names are validated (identifier, keyword, duplicates) and a
   rejected replace/update leaves the previous expression, variables, keyframes
   and unrelated drivers untouched.
5. Bounded, fair socket polling: per-poll caps, queued frames preserved without
   new bytes, loopback-only bind.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import traceback
from pathlib import Path

import bpy

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
REPORT_DIR = TESTS_DIR / "reports"
sys.path.insert(0, str(MCP_DIR / "blender"))

import blender_mcp_addon as addon  # noqa: E402  (after sys.path setup)

REF_TOKEN = "review-regression-token"


class Checker:
    def __init__(self):
        self.checks: list[dict] = []
        self.notes: list[str] = []
        self.started = time.time()

    def check(self, name: str, ok: bool, details=None) -> bool:
        entry = {"name": name, "ok": bool(ok)}
        if details is not None:
            entry["details"] = details
        self.checks.append(entry)
        print("REVIEW::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str))[:600])
        return bool(ok)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print("REVIEW::NOTE::%s" % text)

    @property
    def ok(self) -> bool:
        return all(entry["ok"] for entry in self.checks)

    def summary(self) -> dict:
        failed = [entry["name"] for entry in self.checks if not entry["ok"]]
        return {
            "suite": "blender_review_regressions",
            "blender_version": bpy.app.version_string,
            "python_version": sys.version.split()[0],
            "background": bool(bpy.app.background),
            "factory_startup": bool(bpy.app.factory_startup),
            "duration_s": round(time.time() - self.started, 3),
            "checks_total": len(self.checks),
            "checks_failed": failed,
            "ok": self.ok,
            "notes": self.notes,
            "checks": self.checks,
        }


C = Checker()


def call(command: str, **params):
    return addon.dispatch(command, params)


def error_of(command: str, **params):
    """Return the CommandError from a call that must fail, else None."""
    try:
        call(command, **params)
    except addon.CommandError as exc:
        return exc
    return None


def local_bounds(obj_name: str):
    obj = bpy.data.objects[obj_name]
    origin = obj.matrix_world.translation
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    evaluated = obj.evaluated_get(deps)
    mesh = evaluated.to_mesh()
    try:
        if not mesh.vertices:
            return {"vertices": 0, "min": None, "max": None}
        xs = [v.co.x + origin.x for v in mesh.vertices]
        ys = [v.co.y + origin.y for v in mesh.vertices]
        zs = [v.co.z + origin.z for v in mesh.vertices]
        return {
            "vertices": len(mesh.vertices),
            "min": [round(min(xs), 5), round(min(ys), 5), round(min(zs), 5)],
            "max": [round(max(xs), 5), round(max(ys), 5), round(max(zs), 5)],
        }
    finally:
        evaluated.to_mesh_clear()


def make_mesh_object(name: str, vertices, faces, location=(0.0, 0.0, 0.0)):
    result = call(
        "create_mesh_object",
        name=name,
        vertices=[list(v) for v in vertices],
        faces=[list(f) for f in faces],
        location=list(location),
    )
    return result


def action_fcurves(action) -> list:
    """F-Curves of an action across Blender's legacy and slotted action APIs."""
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


def close(value, expected, tol=1e-3) -> bool:
    try:
        return abs(float(value) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 1. datablock references
# --------------------------------------------------------------------------- #
def test_geometry_group_pointer() -> None:
    """GeometryNodeGroup.node_tree via a structured NODETREE reference."""
    call(
        "create_node_group",
        name="RevInner",
        if_exists="replace",
        interface=[
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "INPUT"},
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
            {"name": "Lift", "socket_type": "NodeSocketFloat", "in_out": "INPUT", "default_value": 2.0},
        ],
    )
    call(
        "batch_graph_edit",
        group="RevInner",
        operations=[
            {"op": "create_node", "node_type": "ShaderNodeCombineXYZ", "name": "LiftVec"},
            {"op": "create_node", "node_type": "GeometryNodeTransform", "name": "Lift"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Lift",
             "to_node": "LiftVec", "to_socket": "Z"},
            {"op": "link_sockets", "from_node": "LiftVec", "from_socket": "Vector",
             "to_node": "Lift", "to_socket": "Translation"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Geometry",
             "to_node": "Lift", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Lift", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    call(
        "create_node_group",
        name="RevOuter",
        if_exists="replace",
        interface=[{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}],
    )
    call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "create_node", "node_type": "GeometryNodeMeshGrid", "name": "Grid"},
            {"op": "create_node", "node_type": "GeometryNodeGroup", "name": "Inner"},
            {"op": "set_socket_default", "node": "Grid", "socket": "Vertices X", "value": 4},
            {"op": "set_socket_default", "node": "Grid", "socket": "Vertices Y", "value": 4},
        ],
    )

    # The group pointer is null until we reference an existing group.
    before = call("inspect_node_group", name="RevOuter")
    inner_node = next(node for node in before["nodes"] if node["name"] == "Inner")
    C.check(
        "group_node_pointer_starts_null",
        inner_node.get("node_tree") is None,
        {"node": inner_node["name"], "node_tree": inner_node.get("node_tree")},
    )

    set_result = call(
        "set_node_property",
        group="RevOuter",
        node="Inner",
        property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevInner"},
    )
    C.check(
        "group_node_pointer_reference_readback",
        isinstance(set_result["value"], dict)
        and set_result["value"].get("id_type") == "NODETREE"
        and set_result["value"].get("id_name") == "RevInner",
        {"value": set_result["value"]},
    )

    # Sockets only exist once the pointer is set, so the graph is linked afterwards.
    linked = call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Inner", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    C.check(
        "nested_group_sockets_exist_after_pointer_set",
        linked["completed_count"] == 2
        and any(node["name"] == "Inner" and node.get("node_tree") == "RevInner"
                for node in call("inspect_node_group", name="RevOuter")["nodes"]),
        {"completed": linked["completed_count"], "failures": linked["failures"]},
    )

    def links_count() -> int:
        return len(call("inspect_node_group", name="RevOuter", include_sockets=False)["links"])

    # Wrong type (a Material is not a NodeTree) must be refused, pointer intact.
    links_before_errors = links_count()
    wrong_type = error_of(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "MATERIAL", "id_name": _ensure_material()},
    )
    after_wrong = call("inspect_node_group", name="RevOuter")
    still = next(node for node in after_wrong["nodes"] if node["name"] == "Inner")
    C.check(
        "group_pointer_wrong_type_refused_and_unchanged",
        wrong_type is not None
        and wrong_type.code == "reference_type_mismatch"
        and still.get("node_tree") == "RevInner"
        and links_count() == links_before_errors,
        {"error": wrong_type.code if wrong_type else None,
         "message": wrong_type.message if wrong_type else None,
         "node_tree_after": still.get("node_tree"),
         "links_before": links_before_errors, "links_after": links_count()},
    )

    # A ShaderNodeTree satisfies the RNA fixed type but breaks the node, so it is
    # rejected explicitly (verified: Blender itself accepts it silently).
    shader = bpy.data.node_groups.new("RevShaderTree", "ShaderNodeTree")
    shader_error = error_of(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": shader.name},
    )
    still_after_shader = next(
        node for node in call("inspect_node_group", name="RevOuter")["nodes"] if node["name"] == "Inner"
    )
    C.check(
        "group_pointer_rejects_shader_tree",
        shader_error is not None
        and shader_error.code == "reference_type_mismatch"
        and still_after_shader.get("node_tree") == "RevInner",
        {"error": shader_error.code if shader_error else None,
         "node_tree_after": still_after_shader.get("node_tree")},
    )

    # Missing datablock must be refused too.
    missing = error_of(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevDoesNotExist"},
    )
    C.check(
        "group_pointer_missing_reference_refused",
        missing is not None and missing.code == "reference_not_found",
        {"error": missing.code if missing else None,
         "available_sample": (missing.details or {}).get("available", [])[:3] if missing else None},
    )

    # A plain string is not accepted as a reference.
    plain = error_of(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree", value="RevInner"
    )
    C.check(
        "group_pointer_plain_string_refused",
        plain is not None and plain.code in ("invalid_property", "invalid_reference"),
        {"error": plain.code if plain else None, "message": plain.message if plain else None},
    )

    # Blender only rebuilds a group node's sockets when the tree actually changes,
    # so the reported link delta must match reality in both cases.
    links_before_drop = links_count()
    same_tree = call(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevInner"},
    )
    same_tree_links = links_count()
    C.check(
        "pointer_reassignment_same_tree_reports_truthfully",
        same_tree["links_before"] == links_before_drop
        and same_tree["links_after"] == same_tree_links
        and bool(same_tree["warnings"]) == (same_tree_links < same_tree["links_before"]),
        {"links_before": same_tree["links_before"], "links_after": same_tree["links_after"],
         "measured_after": same_tree_links, "warnings": same_tree["warnings"]},
    )

    # A different group with a *structurally* different graph: RevInner2 ignores the
    # incoming geometry and outputs a unit cube, so a successful pointer swap is
    # visible in the evaluated mesh regardless of socket-default inheritance.
    call(
        "create_node_group",
        name="RevInner2",
        if_exists="replace",
        interface=[
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "INPUT"},
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
        ],
    )
    call(
        "batch_graph_edit",
        group="RevInner2",
        operations=[
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "Cube"},
            {"op": "link_sockets", "from_node": "Cube", "from_socket": "Mesh",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    swap = call(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevInner2"},
    )
    swap_links = links_count()
    re_linked = call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Inner", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    C.check(
        "pointer_swap_to_other_group_is_reported_truthfully",
        swap["links_before"] == links_before_drop
        and swap["links_after"] == swap_links
        and swap["value"]["id_name"] == "RevInner2"
        and bool(swap["warnings"]) == (swap_links < swap["links_before"])
        and re_linked["completed_count"] == 2,
        {"links_before": swap["links_before"], "links_after": swap["links_after"],
         "measured_after": swap_links, "warnings": swap["warnings"],
         "linked_after_swap": re_linked["completed_count"]},
    )
    # Back to the original nested group; the evaluated effect of the swap is
    # verified in test_nested_group_reuse_evaluated (which owns the object).
    swap_back = call(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevInner"},
    )
    call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Inner", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    C.check(
        "pointer_swap_back_to_original_group",
        swap_back["value"]["id_name"] == "RevInner" and links_count() == 2,
        {"value": swap_back["value"], "links": links_count()},
    )

    # Clearing with null is legal, and re-setting reuses the existing group.
    cleared = call("set_node_property", group="RevOuter", node="Inner", property="node_tree", value=None)
    node_groups_before = len(bpy.data.node_groups)
    reset = call(
        "set_node_property", group="RevOuter", node="Inner", property="node_tree",
        value={"id_type": "NODETREE", "id_name": "RevInner"},
    )
    call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Inner", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    C.check(
        "group_pointer_clear_and_reuse_without_clone",
        cleared["value"] is None
        and reset["value"]["id_name"] == "RevInner"
        and len(bpy.data.node_groups) == node_groups_before
        and bpy.data.node_groups["RevInner"].users >= 1,
        {"cleared": cleared["value"], "reset": reset["value"],
         "node_groups": node_groups_before, "inner_users": bpy.data.node_groups["RevInner"].users},
    )


def _ensure_material() -> str:
    material = bpy.data.materials.get("RevMaterial") or bpy.data.materials.new("RevMaterial")
    return material.name


def test_nested_group_reuse_evaluated() -> None:
    """The nested group must drive real evaluated geometry on an object."""
    make_mesh_object("RevPanel", [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [[0, 1, 2, 3]])
    attach = call(
        "attach_geometry_nodes_modifier",
        object="RevPanel",
        group="RevOuter",
        modifier_name="RevOuter",
    )
    bounds = local_bounds("RevPanel")
    C.check(
        "nested_group_reuse_evaluates_geometry",
        attach["evaluated"]["evaluated"]["vertices"] == 16
        and bounds["vertices"] == 16
        and close(bounds["max"][2], 2.0),
        {"attach_evaluated": attach["evaluated"]["evaluated"], "bounds": bounds,
         "note": "outer Grid(4x4) -> inner group Lifts Z by 2"},
    )

    # Re-attaching preserves the same modifier (procedural, additive, no clone).
    again = call(
        "attach_geometry_nodes_modifier",
        object="RevPanel",
        group="RevOuter",
        modifier_name="RevOuter",
        reuse_existing=True,
    )
    C.check(
        "attach_reuses_existing_modifier",
        again["created"] is False and len([m for m in bpy.data.objects["RevPanel"].modifiers if m.type == "NODES"]) == 1,
        {"created": again["created"],
         "modifiers": [m.name for m in bpy.data.objects["RevPanel"].modifiers]},
    )

    # Swapping the nested group's pointer must change the evaluated result, and
    # swapping back must restore it (links are re-made after each socket rebuild).
    def swap_and_relink(target: str):
        result = call("set_node_property", group="RevOuter", node="Inner", property="node_tree",
                      value={"id_type": "NODETREE", "id_name": target})
        relinked = call(
            "batch_graph_edit",
            group="RevOuter",
            operations=[
                {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
                 "to_node": "Inner", "to_socket": "Geometry"},
                {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
                 "to_node": "Group Output", "to_socket": "Geometry"},
            ],
        )
        return result, relinked

    to_other, relink_other = swap_and_relink("RevInner2")
    swapped_bounds = local_bounds("RevPanel")
    back_to_inner, relink_back = swap_and_relink("RevInner")
    restored_bounds = local_bounds("RevPanel")
    C.check(
        "nested_pointer_swap_changes_evaluated_geometry",
        to_other["value"]["id_name"] == "RevInner2"
        and relink_other["completed_count"] == 2
        and swapped_bounds["vertices"] == 8
        and close(swapped_bounds["max"][2], 0.5)
        and back_to_inner["value"]["id_name"] == "RevInner"
        and relink_back["completed_count"] == 2
        and restored_bounds["vertices"] == 16
        and close(restored_bounds["max"][2], 2.0),
        {"swapped": swapped_bounds, "restored": restored_bounds,
         "to_other": to_other["value"], "back_to_inner": back_to_inner["value"],
         "note": "RevInner2 replaces the geometry with a unit cube; RevInner lifts the grid by 2"},
    )

    # unlink_socket must really remove exactly the addressed link (this used to be
    # silently a no-op because bpy wrappers are not identity-comparable).
    unlinked = call("unlink_socket", group="RevOuter", node="Inner", socket="Geometry", in_out="INPUT")
    empty_bounds = local_bounds("RevPanel")
    relinked = call(
        "batch_graph_edit",
        group="RevOuter",
        operations=[
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Inner", "to_socket": "Geometry"},
            {"op": "link_sockets", "from_node": "Inner", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )
    repaired_bounds = local_bounds("RevPanel")
    C.check(
        "unlink_socket_removes_exactly_one_link_and_changes_geometry",
        unlinked["removed_count"] == 1
        and empty_bounds["vertices"] == 0
        and relinked["completed_count"] == 2
        and repaired_bounds["vertices"] == 16
        and close(repaired_bounds["max"][2], 2.0),
        {"removed": unlinked["removed"], "removed_count": unlinked["removed_count"],
         "bounds_without_link": empty_bounds, "bounds_after_relink": repaired_bounds},
    )


def test_object_info_reference() -> None:
    """Object Info socket default + modifier pointer parameter + interface default."""
    make_mesh_object(
        "RevSource",
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0.5, 0.5, 1)],
        [[0, 1, 2, 3], [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]],
        location=(7.0, 0.0, 0.0),
    )
    source_vertices = len(bpy.data.objects["RevSource"].data.vertices)
    make_mesh_object(
        "RevSource2",
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (2, 2, 0)],
        [[0, 1, 2], [1, 2, 3]],
        location=(9.0, 0.0, 0.0),
    )

    # Interface pointer default referencing an existing object.
    group = call(
        "create_node_group",
        name="RevObjectInfo",
        if_exists="replace",
        interface=[
            {"name": "Ref", "socket_type": "NodeSocketObject", "in_out": "INPUT",
             "default_value": {"id_type": "OBJECT", "id_name": "RevSource"}},
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
        ],
    )
    interface_input = group["group"]["interface"]["inputs"][0]
    C.check(
        "interface_pointer_default_reference_readback",
        interface_input["default_value"] == {"id_type": "OBJECT", "id_name": "RevSource", "rna_type": "Object"}
        or (isinstance(interface_input["default_value"], dict)
            and interface_input["default_value"].get("id_name") == "RevSource"),
        {"interface_input": interface_input},
    )

    call(
        "batch_graph_edit",
        group="RevObjectInfo",
        operations=[
            {"op": "create_node", "node_type": "GeometryNodeObjectInfo", "name": "Info"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Ref",
             "to_node": "Info", "to_socket": "Object"},
            {"op": "link_sockets", "from_node": "Info", "from_socket": "Geometry",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    )

    # Socket default route (direct socket default, not the modifier parameter).
    socket_result = call(
        "set_socket_default",
        group="RevObjectInfo",
        node="Info",
        socket="Object",
        value={"id_type": "OBJECT", "id_name": "RevSource"},
    )
    C.check(
        "socket_pointer_default_reference_readback",
        isinstance(socket_result["value"], dict) and socket_result["value"].get("id_name") == "RevSource",
        {"value": socket_result["value"]},
    )
    cleared_socket = call(
        "set_socket_default", group="RevObjectInfo", node="Info", socket="Object", value=None
    )
    C.check(
        "socket_pointer_clear_with_null",
        cleared_socket["value"] is None,
        {"value": cleared_socket["value"]},
    )
    call(
        "set_socket_default", group="RevObjectInfo", node="Info", socket="Object",
        value={"id_type": "OBJECT", "id_name": "RevSource"},
    )

    make_mesh_object("RevConsumer", [(-1, -1, 0), (1, -1, 0), (1, 1, 0)], [[0, 1, 2]], location=(-7.0, 0.0, 0.0))
    attach = call(
        "attach_geometry_nodes_modifier",
        object="RevConsumer",
        group="RevObjectInfo",
        modifier_name="RevObjectInfo",
        parameters={"Ref": {"id_type": "OBJECT", "id_name": "RevSource"}},
    )
    bounds = local_bounds("RevConsumer")
    C.check(
        "modifier_pointer_parameter_reference",
        bounds["vertices"] == source_vertices
        and attach["parameters_applied"]
        and isinstance(attach["parameters_applied"][0].get("reference"), dict)
        and attach["parameters_applied"][0]["reference"]["id_name"] == "RevSource",
        {"bounds": bounds, "source_vertices": source_vertices,
         "applied": attach["parameters_applied"]},
    )

    parameters = call("set_modifier_input", object="RevConsumer", input="Ref",
                      value={"id_type": "OBJECT", "id_name": "RevSource2"})
    swapped = local_bounds("RevConsumer")
    C.check(
        "modifier_pointer_parameter_switch_changes_geometry",
        parameters["parameters"]["Socket_0"]["reference"]["id_name"] == "RevSource2"
        and swapped["vertices"] == 4,
        {"parameters": parameters["parameters"], "bounds": swapped,
         "note": "RevSource2 has 4 vertices"},
    )

    # Plain values are refused for pointer parameters (Blender's IDProperty store
    # would silently accept a string that names no datablock).
    plain = error_of("set_modifier_input", object="RevConsumer", input="Ref", value="RevSource")
    C.check(
        "modifier_pointer_parameter_rejects_plain_string",
        plain is not None and plain.code == "invalid_modifier_input"
        and "invalid_reference" in json.dumps(plain.details, default=str),
        {"error": plain.code if plain else None, "details": plain.details if plain else None},
    )
    still = call("object_info", name="RevConsumer")
    C.check(
        "modifier_pointer_plain_string_left_state_unchanged",
        still["modifiers"][0]["parameters"]["Socket_0"]["reference"]["id_name"] == "RevSource2",
        {"parameters": still["modifiers"][0]["parameters"]},
    )

    cleared = call("set_modifier_input", object="RevConsumer", input="Ref", value=None)
    empty = local_bounds("RevConsumer")
    C.check(
        "modifier_pointer_parameter_clear_with_null",
        cleared["parameters"]["Socket_0"]["reference"] is None and empty["vertices"] == 0,
        {"parameters": cleared["parameters"], "bounds": empty},
    )
    call("set_modifier_input", object="RevConsumer", input="Ref",
         value={"id_type": "OBJECT", "id_name": "RevSource"})

    # Missing / wrong-type references are refused without touching the pointer.
    missing = error_of("set_modifier_input", object="RevConsumer", input="Ref",
                       value={"id_type": "OBJECT", "id_name": "RevNope"})
    wrong = error_of("set_modifier_input", object="RevConsumer", input="Ref",
                     value={"id_type": "MATERIAL", "id_name": _ensure_material()})
    unsupported = error_of("set_modifier_input", object="RevConsumer", input="Ref",
                           value={"id_type": "SCENE", "id_name": bpy.context.scene.name})
    after_errors = call("object_info", name="RevConsumer")
    C.check(
        "modifier_pointer_errors_leave_state_unchanged",
        missing is not None and missing.code == "invalid_modifier_input"
        and wrong is not None and "reference_type_mismatch" in json.dumps(wrong.details, default=str)
        and unsupported is not None and "invalid_reference" in json.dumps(unsupported.details, default=str)
        and after_errors["modifiers"][0]["parameters"]["Socket_0"]["reference"]["id_name"] == "RevSource",
        {"missing": missing.code if missing else None,
         "wrong": wrong.code if wrong else None,
         "unsupported": unsupported.code if unsupported else None,
         "parameters": after_errors["modifiers"][0]["parameters"]},
    )


# --------------------------------------------------------------------------- #
# 2. non-finite shape key coordinates
# --------------------------------------------------------------------------- #
def test_non_finite_shape_key_coordinates() -> None:
    make_mesh_object("RevMorph", [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [[0, 1, 2, 3]])
    keys = call("create_shape_key", object="RevMorph", name="Wide", value=0.0)
    vertex_count = keys["mesh_vertices"]
    good = [[0.5, 0.0, 0.0] for _ in range(vertex_count)]
    call("set_shape_key_coordinates", object="RevMorph", key_name="Wide", deltas=good)

    block = bpy.data.objects["RevMorph"].data.shape_keys.key_blocks["Wide"]
    before = [list(block.data[i].co) for i in range(vertex_count)]

    nan = json.loads("NaN")          # exactly what a JSON "NaN" token becomes
    infinity = json.loads("Infinity")
    C.check(
        "nan_is_reachable_from_json",
        nan != nan and infinity == float("inf"),
        {"nan_repr": repr(nan), "infinity_repr": repr(infinity)},
    )

    payload_nan = [list(point) for point in before]
    payload_nan[2] = [nan, 0.0, 0.0]
    nan_error = error_of(
        "set_shape_key_coordinates", object="RevMorph", key_name="Wide", coordinates=payload_nan
    )
    after_nan = [list(block.data[i].co) for i in range(vertex_count)]

    payload_inf = [list(point) for point in before]
    payload_inf[0] = [0.0, infinity, 0.0]
    inf_error = error_of(
        "set_shape_key_coordinates", object="RevMorph", key_name="Wide", deltas=payload_inf
    )
    after_inf = [list(block.data[i].co) for i in range(vertex_count)]

    C.check(
        "non_finite_shape_key_coordinates_rejected",
        nan_error is not None and nan_error.code == "non_finite_value"
        and inf_error is not None and inf_error.code == "non_finite_value",
        {"nan": nan_error.code if nan_error else None,
         "infinity": inf_error.code if inf_error else None,
         "nan_message": nan_error.message if nan_error else None},
    )
    C.check(
        "non_finite_rejection_leaves_coordinates_unchanged",
        after_nan == before and after_inf == before,
        {"sample_before": before[:2], "sample_after_nan": after_nan[:2],
         "sample_after_inf": after_inf[:2]},
    )


# --------------------------------------------------------------------------- #
# 3. driver replace/update rollback
# --------------------------------------------------------------------------- #
def test_driver_replace_rollback() -> None:
    make_mesh_object("RevDriver", [(0, 0, 0), (1, 0, 0), (1, 1, 0)], [[0, 1, 2]], location=(0.0, 6.0, 0.0))
    call("set_custom_property", owner_kind="object", owner_name="RevDriver", key="phase", value=0.25)
    obj = bpy.data.objects["RevDriver"]

    # Unrelated animation + an unrelated driver that must survive every failure.
    obj.location[0] = 0.0
    obj.keyframe_insert(data_path="location", index=0, frame=1)
    call("set_custom_property", owner_kind="object", owner_name="RevDriver", key="lift", value=1.0)
    call("add_driver", owner_kind="object", owner_name="RevDriver", data_path="scale", index=1,
         expression="lift",
         variables=[{"name": "lift", "type": "SINGLE_PROP",
                     "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["lift"]'}]}])

    call("add_driver", owner_kind="object", owner_name="RevDriver", data_path="rotation_euler", index=2,
         expression="phase * 2",
         variables=[{"name": "phase", "type": "SINGLE_PROP",
                     "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}])
    call("update_depsgraph")

    def snapshot():
        drivers = call("inspect_drivers", owner_kind="object", owner_name="RevDriver")
        rotation = next(d for d in drivers["drivers"] if d["data_path"] == "rotation_euler")
        scale = next(d for d in drivers["drivers"] if d["data_path"] == "scale")
        animation = getattr(obj, "animation_data", None)
        # Keyframes live on the action (slotted-action API in Blender 5.x), not in
        # animation_data.drivers, so count them there.
        keyed = [
            (fcurve.data_path, fcurve.array_index, len(fcurve.keyframe_points))
            for fcurve in action_fcurves(animation.action if animation else None)
            if len(fcurve.keyframe_points) > 0
        ]
        return {
            "rotation_expression": rotation["expression"],
            "rotation_variables": [(v["name"], v["type"]) for v in rotation["variables"]],
            "rotation_valid": rotation["is_valid"],
            "scale_expression": scale["expression"],
            "keyed_curves": keyed,
            "rotation_value": round(float(obj.rotation_euler[2]), 6),
        }

    before = snapshot()
    C.check(
        "driver_baseline_ready",
        before["rotation_expression"] == "phase * 2"
        and before["rotation_value"] == 0.5
        and before["keyed_curves"] == [("location", 0, 1)],
        before,
    )

    # a) replace=True with a semantically invalid expression.
    replace_error = error_of(
        "add_driver", owner_kind="object", owner_name="RevDriver", data_path="rotation_euler", index=2,
        expression="1 / (phase - phase)", replace=True,
        variables=[{"name": "phase", "type": "SINGLE_PROP",
                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}],
    )
    after_replace = snapshot()
    C.check(
        "rejected_replace_restores_expression_variables_result",
        replace_error is not None and replace_error.code == "invalid_expression"
        and after_replace == before,
        {"error": replace_error.code if replace_error else None,
         "rollback": (replace_error.details or {}).get("rollback") if replace_error else None,
         "before": before, "after": after_replace},
    )

    # b) update with a semantically invalid expression.
    update_error = error_of(
        "update_driver", owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
        index=2, expression="phase / (phase - phase)",
    )
    after_update = snapshot()
    C.check(
        "rejected_update_restores_expression_variables_result",
        update_error is not None and update_error.code == "invalid_expression" and after_update == before,
        {"error": update_error.code if update_error else None,
         "restored_expression": (update_error.details or {}).get("restored_expression") if update_error else None,
         "before": before, "after": after_update},
    )

    # c) update with a syntactically broken expression (AST validation).
    syntax_error = error_of(
        "update_driver", owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
        index=2, expression="phase +",
    )
    C.check(
        "invalid_syntax_update_refused_before_edit",
        syntax_error is not None and syntax_error.code == "invalid_expression" and snapshot() == before,
        {"error": syntax_error.code if syntax_error else None, "after": snapshot()},
    )

    exec_error = error_of(
        "add_driver", owner_kind="object", owner_name="RevDriver", data_path="location", index=0,
        expression="__import__('os').system('echo pwned')",
    )
    C.check(
        "ast_expression_validation_still_blocks_python",
        exec_error is not None and exec_error.code == "invalid_expression" and snapshot() == before,
        {"error": exec_error.message if exec_error else None},
    )

    # d) variable-name validation before any driver edit.
    cases = {
        "duplicate": (
            "add_driver", dict(owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
                               index=0, expression="a + b",
                               variables=[
                                   {"name": "a", "type": "SINGLE_PROP",
                                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]},
                                   {"name": "a", "type": "SINGLE_PROP",
                                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["lift"]'}]},
                               ]),
            "duplicate_variable",
        ),
        "bad_identifier": (
            "add_driver", dict(owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
                               index=0, expression="2bad", variables=[
                                   {"name": "2bad", "type": "SINGLE_PROP",
                                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}]),
            "invalid_variable_name",
        ),
        "keyword": (
            "add_driver", dict(owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
                               index=0, expression="class", variables=[
                                   {"name": "class", "type": "SINGLE_PROP",
                                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}]),
            "invalid_variable_name",
        ),
        "dunder": (
            "add_driver", dict(owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
                               index=0, expression="__x", variables=[
                                   {"name": "__x", "type": "SINGLE_PROP",
                                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}]),
            "invalid_variable_name",
        ),
        "update_duplicate": (
            "update_driver", dict(owner_kind="object", owner_name="RevDriver", data_path="rotation_euler",
                                  index=2, variables=[
                                      {"name": "phase", "type": "SINGLE_PROP",
                                       "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]},
                                      {"name": "phase", "type": "SINGLE_PROP",
                                       "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["lift"]'}]}]),
            "duplicate_variable",
        ),
    }
    name_results = {}
    for label, (command, params, expected) in cases.items():
        started = snapshot()
        error = error_of(command, **params)
        name_results[label] = {
            "code": error.code if error else None,
            "expected": expected,
            "unchanged": snapshot() == started,
        }
    C.check(
        "driver_variable_names_validated_before_edit",
        all(entry["code"] == entry["expected"] and entry["unchanged"] for entry in name_results.values()),
        name_results,
    )

    # e) successful replacement still works and keeps unrelated animation.
    ok_replace = call(
        "add_driver", owner_kind="object", owner_name="RevDriver", data_path="rotation_euler", index=2,
        expression="phase * 4", replace=True,
        variables=[{"name": "phase", "type": "SINGLE_PROP",
                    "targets": [{"id_type": "OBJECT", "id_name": "RevDriver", "data_path": '["phase"]'}]}],
    )
    call("update_depsgraph")
    after_ok = snapshot()
    C.check(
        "successful_replace_applies_and_keeps_unrelated_animation",
        ok_replace["channels"][0]["is_valid"] is True
        and after_ok["rotation_expression"] == "phase * 4"
        and close(after_ok["rotation_value"], 1.0)
        and after_ok["scale_expression"] == "lift"
        and after_ok["keyed_curves"] == [("location", 0, 1)],
        {"after": after_ok, "channels": ok_replace["channels"]},
    )


# --------------------------------------------------------------------------- #
# 4. bounded polling + loopback
# --------------------------------------------------------------------------- #
def _raw_ping(token: str, request_id: str) -> bytes:
    return (json.dumps({"id": request_id, "token": token, "command": "ping", "params": {}}) + "\n").encode()


def _read_frames(sock, expected: int, deadline: float) -> list[dict]:
    buffer = b""
    frames = []
    while time.time() < deadline and len(frames) < expected:
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, TimeoutError):
            continue
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            if line.strip():
                frames.append(json.loads(line.decode("utf-8")))
    return frames


def test_loopback_only_bind() -> None:
    accepted = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "LOCALHOST": "127.0.0.1",
                "127.0.0.5": "127.0.0.5", "::1": "::1", "[::1]": "::1"}
    rejected = ["0.0.0.0", "192.168.1.10", "::", "[::]", "example.com", "", None, "10.0.0.1"]
    normalize_results = {
        "accepted": {host: addon.normalize_loopback_host(host) for host in accepted},
        "rejected": {repr(host): addon.normalize_loopback_host(host) for host in rejected},
    }
    C.check(
        "loopback_host_normalization",
        all(normalize_results["accepted"][host] == expected for host, expected in accepted.items())
        and all(normalize_results["rejected"][repr(host)] is None for host in rejected),
        normalize_results,
    )

    bind_results = {}
    for host in ("0.0.0.0", "192.168.1.10", "example.com"):
        server = addon.BridgeServer(host, 0, REF_TOKEN, lambda message: None)
        error = None
        try:
            server.start()
        except addon.CommandError as exc:
            error = exc
        finally:
            server.stop("loopback rejection test")
        bind_results[host] = error.code if error else "STARTED"
    check = addon.BridgeServer("0.0.0.0", 0, REF_TOKEN, lambda message: None)
    check_error = None
    try:
        check.start()
    except addon.CommandError as exc:
        check_error = exc
    C.check(
        "non_loopback_bind_rejected_before_start",
        all(value == "invalid_host" for value in bind_results.values())
        and check_error is not None
        and check_error.code == "invalid_host"
        and check.running is False,
        {"binds": bind_results, "running": check.running,
         "message": check_error.message if check_error else None},
    )


def test_bounded_fair_polling() -> None:
    saved = {
        "MAX_MESSAGES_PER_POLL": addon.MAX_MESSAGES_PER_POLL,
        "MAX_MESSAGES_PER_POLL_PER_CLIENT": addon.MAX_MESSAGES_PER_POLL_PER_CLIENT,
        "MAX_BYTES_PER_POLL_PER_CLIENT": addon.MAX_BYTES_PER_POLL_PER_CLIENT,
        "MAX_ACCEPTS_PER_POLL": addon.MAX_ACCEPTS_PER_POLL,
        "MAX_CLIENTS": addon.MAX_CLIENTS,
    }
    addon.MAX_MESSAGES_PER_POLL = 2
    addon.MAX_MESSAGES_PER_POLL_PER_CLIENT = 1
    addon.MAX_BYTES_PER_POLL_PER_CLIENT = 4096
    addon.MAX_ACCEPTS_PER_POLL = 1
    addon.MAX_CLIENTS = 2
    server = addon.BridgeServer("127.0.0.1", 0, REF_TOKEN, lambda message: None)
    server.start()
    port = server._sock.getsockname()[1]
    sockets = []
    try:
        # Three connections: the third must be refused (client cap = 2).
        clients = []
        for index in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
            sock.settimeout(0.5)
            clients.append(sock)
            sockets.append(sock)
        server.poll()  # one accept only
        accepted_after_one_poll = len(server._clients)
        for _ in range(6):
            server.poll()
        refused = _read_frames(clients[2], 1, time.time() + 2.0)
        C.check(
            "accept_is_capped_and_client_limit_enforced",
            accepted_after_one_poll == 1
            and any(frame.get("error", {}).get("code") == "too_many_clients" for frame in refused),
            {"accepted_after_one_poll": accepted_after_one_poll,
             "clients": len(server._clients), "refused": refused},
        )

        # Five pipelined frames from client A in a single send, one from client B.
        clients[0].sendall(b"".join(_raw_ping(REF_TOKEN, "a%d" % index) for index in range(5)))
        clients[1].sendall(_raw_ping(REF_TOKEN, "b0"))

        poll_counts = []
        frames_a: list[dict] = []
        frames_b: list[dict] = []
        b_first_poll = None
        deadline = time.time() + 5.0
        polls = 0
        while polls < 40 and time.time() < deadline:
            polls += 1
            before = len(frames_a) + len(frames_b)
            server.poll()
            # No new bytes are ever sent from here on: queued frames must drain.
            frames_a.extend(_read_frames(clients[0], 5 - len(frames_a), time.time() + 0.15))
            frames_b.extend(_read_frames(clients[1], 1 - len(frames_b), time.time() + 0.05))
            produced = len(frames_a) + len(frames_b) - before
            poll_counts.append(produced)
            if frames_b and b_first_poll is None:
                b_first_poll = polls
            if len(frames_a) >= 5 and len(frames_b) >= 1:
                break
        C.check(
            "pipelined_frames_are_processed_in_bounded_batches",
            all(count <= addon.MAX_MESSAGES_PER_POLL for count in poll_counts)
            and max(poll_counts) > 0,
            {"per_poll_responses": poll_counts, "cap": addon.MAX_MESSAGES_PER_POLL},
        )
        C.check(
            "queued_frames_drain_without_new_bytes",
            len(frames_a) == 5 and len(frames_b) == 1 and polls > 1,
            {"client_a": [f.get("id") for f in frames_a], "client_b": [f.get("id") for f in frames_b],
             "polls": polls},
        )
        C.check(
            "no_client_is_starved_by_another",
            b_first_poll is not None and b_first_poll <= 2,
            {"client_b_answered_on_poll": b_first_poll, "per_poll_responses": poll_counts},
        )

        # Oversized frame: explicit error, then the connection is closed.
        small_log: list[str] = []
        small = addon.BridgeServer("127.0.0.1", 0, REF_TOKEN, small_log.append, max_frame=1024)
        small.start()
        sockets.append(small._sock)
        small_port = small._sock.getsockname()[1]
        big = socket.create_connection(("127.0.0.1", small_port), timeout=5.0)
        big.settimeout(0.5)
        sockets.append(big)
        big.sendall(b'{"token": "' + REF_TOKEN.encode() + b'", "command": "ping", "pad": "' + b"a" * 8192 + b'"}\n')
        oversized_frames = []
        raw_tail = b""
        deadline = time.time() + 4.0
        while time.time() < deadline and not oversized_frames:
            small.poll()
            try:
                chunk = big.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                raw_tail += ("OSError %s" % exc).encode()
                break
            if not chunk:
                break
            raw_tail += chunk
            while b"\n" in raw_tail:
                line, _, raw_tail = raw_tail.partition(b"\n")
                if line.strip():
                    oversized_frames.append(json.loads(line.decode("utf-8")))
        closed = False
        for _ in range(10):
            small.poll()
            try:
                if not big.recv(65536):
                    closed = True
                    break
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                closed = True
                break
        C.check(
            "oversized_frame_still_errors_then_closes",
            bool(oversized_frames)
            and oversized_frames[0]["error"]["code"] == "frame_too_large"
            and closed,
            {"frames": oversized_frames, "closed": closed, "raw_tail": repr(raw_tail[:200]),
             "server_status": small.status(), "server_log": small_log[-5:]},
        )
        small.stop("regression teardown")
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        server.stop("regression teardown")
        for key, value in saved.items():
            setattr(addon, key, value)


# --------------------------------------------------------------------------- #
def reset_scene() -> None:
    for obj in list(bpy.data.objects):
        if obj.name.startswith("Rev") or obj.name == "Cube":
            bpy.data.objects.remove(obj, do_unlink=True)
    for group in list(bpy.data.node_groups):
        if group.name.startswith("Rev"):
            bpy.data.node_groups.remove(group)
    for material in list(bpy.data.materials):
        if material.name.startswith("Rev"):
            bpy.data.materials.remove(material)


def main() -> int:
    try:
        reset_scene()
        test_geometry_group_pointer()
        test_nested_group_reuse_evaluated()
        test_object_info_reference()
        test_non_finite_shape_key_coordinates()
        test_driver_replace_rollback()
        test_loopback_only_bind()
        test_bounded_fair_polling()
    except Exception as exc:  # noqa: BLE001 - report everything
        C.check("regression_run_completed", False,
                {"exception": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()})

    payload = C.summary()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "review_report.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("REVIEW::SUMMARY::" + json.dumps(
        {"ok": payload["ok"], "checks": payload["checks_total"], "failed": payload["checks_failed"],
         "report": str(REPORT_DIR / "review_report.json")}))
    return 0 if C.ok else 1


if __name__ == "__main__":
    code = main()
    print("REVIEW::EXIT::%d" % code)
    if code != 0:
        raise SystemExit(code)
