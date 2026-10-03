"""Eighth probe: exact tagging needed for shape keys and node-group edits."""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE8::%s::%s" % (title, json.dumps(payload, default=str)))


def stats(obj, tag=None):
    if tag == "obj":
        obj.update_tag()
    elif tag == "key":
        obj.data.shape_keys.update_tag()
    elif tag == "mesh":
        obj.data.update_tag()
    elif tag == "group":
        for mod in obj.modifiers:
            if mod.type == "NODES" and mod.node_group:
                mod.node_group.update_tag()
    elif tag == "objgroup":
        obj.update_tag()
        for mod in obj.modifiers:
            if mod.type == "NODES" and mod.node_group:
                mod.node_group.update_tag()
    elif tag == "viewlayer":
        bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
    ev = obj.evaluated_get(deps)
    mesh = ev.to_mesh()
    try:
        xs = [round(v.co.x, 4) for v in mesh.vertices]
        return {"n": len(mesh.vertices), "min_x": min(xs), "max_x": max(xs)}
    finally:
        ev.to_mesh_clear()


def base_mesh(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main():
    # ---------- shape keys -------------------------------------------------- #
    obj = base_mesh("SK")
    obj.shape_key_add(name="Basis")
    kb = obj.shape_key_add(name="Wide")
    for point in kb.data:
        point.co = (point.co.x + 1.0, point.co.y, point.co.z)
    report("sk_after_coordinates_before_value", stats(obj, "obj"))
    report("sk_value_0", stats(obj, "obj"))

    sequences = {}
    for tag in ("none", "obj", "key", "mesh", "viewlayer"):
        kb.value = 1.0
        # make sure the previous scenario cannot bleed in
        stats(obj, "obj")
        kb.value = 1.0
        sequences[tag] = stats(obj, None if tag == "none" else tag)
        kb.value = 0.0
        stats(obj, "obj")
    report("shape_key_value_tag_strategies", sequences)

    # coordinate edit + value already 1
    kb.value = 1.0
    stats(obj, "obj")
    before = stats(obj, "obj")
    for point in kb.data:
        point.co = (point.co.x + 1.0, point.co.y, point.co.z)
    after_none = stats(obj)
    after_obj = stats(obj, "obj")
    kb.value = 0.0
    stats(obj, "obj")
    report("shape_key_coordinate_edit", {"before": before, "after_no_tag": after_none, "after_obj_tag": after_obj})

    # driver on the key value
    obj["bulge"] = 0.0
    fcu = obj.data.shape_keys.driver_add('key_blocks["Wide"].value')
    fcu.driver.expression = "bulge"
    var = fcu.driver.variables.new()
    var.name = "bulge"
    var.targets[0].id_type = "OBJECT"
    var.targets[0].id = obj
    var.targets[0].data_path = '["bulge"]'
    obj["bulge"] = 1.0
    report("sk_driver_no_tag", stats(obj))
    report("sk_driver_obj_tag", stats(obj, "obj"))
    report("sk_driver_viewlayer", stats(obj, "viewlayer"))

    # ---------- node group socket default ---------------------------------- #
    ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    grid = ng.nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Vertices X"].default_value = 4
    grid.inputs["Vertices Y"].default_value = 4
    ng.links.new(grid.outputs["Mesh"], go.inputs["Geometry"])
    o2 = base_mesh("GN")
    mod = o2.modifiers.new("GN", "NODES")
    mod.node_group = ng
    report("gn_baseline", stats(o2, "obj"))

    seq = {}
    for tag in ("none", "obj", "group", "objgroup", "viewlayer"):
        grid.inputs["Vertices Y"].default_value = 2
        stats(o2, "objgroup")
        grid.inputs["Vertices Y"].default_value = 2
        seq[tag] = stats(o2, None if tag == "none" else tag)
        grid.inputs["Vertices Y"].default_value = 4
        stats(o2, "objgroup")
    report("node_socket_default_tag_strategies", seq)

    # node rename inside a group: does an existing link follow?
    report("link_after_rename_check", {"note": "not tested here"})

    # driver on the socket default
    bpy.context.scene["rows"] = 3
    fcu2 = ng.driver_add('nodes["Grid"].inputs["Vertices Y"].default_value')
    fcu2.driver.expression = "rows"
    var2 = fcu2.driver.variables.new()
    var2.name = "rows"
    var2.targets[0].id_type = "SCENE"
    var2.targets[0].id = bpy.context.scene
    var2.targets[0].data_path = '["rows"]'
    report("ng_driver_after_add", stats(o2, "objgroup"))
    bpy.context.scene["rows"] = 5
    report("ng_driver_no_tag", stats(o2))
    report("ng_driver_objtag", stats(o2, "obj"))
    report("ng_driver_frame", (bpy.context.scene.frame_set(2), stats(o2, "obj"))[1])

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
