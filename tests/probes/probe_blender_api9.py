"""Ninth probe: decisive, isolated flush tests (one change per measurement)."""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE9::%s::%s" % (title, json.dumps(payload, default=str)))


def measure(obj, tags=()):
    for tag in tags:
        if tag == "obj":
            obj.update_tag()
        elif tag == "mesh":
            obj.data.update_tag()
        elif tag == "key":
            obj.data.shape_keys.update_tag()
        elif tag == "scene":
            bpy.context.scene.update_tag()
        elif tag == "group":
            for mod in obj.modifiers:
                if mod.type == "NODES" and mod.node_group:
                    mod.node_group.update_tag()
        elif tag == "viewlayer":
            bpy.context.view_layer.update()
        elif tag == "frame":
            bpy.context.scene.frame_set(bpy.context.scene.frame_current + 1)
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
    # ---------------- shape key value vs flush ---------------------------- #
    obj = base_mesh("SK")
    obj.shape_key_add(name="Basis")
    kb = obj.shape_key_add(name="Wide")
    for point in kb.data:
        point.co = (point.co.x + 10.0, point.co.y, point.co.z)
    result = {}
    for tags in ((), ("obj",), ("mesh",), ("key",), ("viewlayer",)):
        # settle at value 0 first (Basis: 0..1)
        kb.value = 0.0
        measure(obj, ("obj",))
        baseline = measure(obj, ("obj",))
        # now the change under test
        kb.value = 1.0
        result["+".join(tags) or "none"] = {"value0": baseline, "value1": measure(obj, tags)}
        kb.value = 0.0
        measure(obj, ("obj",))
    report("shape_key_value_flush", result)

    # ---------------- node tree driver flush ------------------------------ #
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
    report("gn_baseline", measure(o2, ("obj",)))

    scene = bpy.context.scene
    scene["rows"] = 3
    fcu = ng.driver_add('nodes["Grid"].inputs["Vertices Y"].default_value')
    fcu.driver.expression = "rows"
    var = fcu.driver.variables.new()
    var.name = "rows"
    var.targets[0].id_type = "SCENE"
    var.targets[0].id = scene
    var.targets[0].data_path = '["rows"]'
    report("driver_added", {"driver_valid": fcu.driver.is_valid, "measured": measure(o2, ("obj", "group"))})

    results = {}
    for tags in ((), ("obj",), ("group",), ("scene",), ("viewlayer",), ("frame",), ("obj", "group")):
        scene["rows"] = 7
        measure(o2, ("obj", "group", "scene"))
        scene["rows"] = 7
        results["+".join(tags) or "none"] = measure(o2, tags)
        scene["rows"] = 3
        measure(o2, ("obj", "group", "scene"))
    report("node_tree_driver_flush", results)
    report("expected_vertices_for_rows7", 4 * 7)

    # ---------------- modifier parameter recap ---------------------------- #
    ng2 = bpy.data.node_groups.new("G2", "GeometryNodeTree")
    ng2.interface.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat").default_value = 2.0
    ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng2.nodes.new("NodeGroupInput")
    go2 = ng2.nodes.new("NodeGroupOutput")
    grid2 = ng2.nodes.new("GeometryNodeMeshGrid")
    grid2.inputs["Vertices X"].default_value = 2
    grid2.inputs["Vertices Y"].default_value = 2
    ng2.links.new(ng2.nodes["Group Input"].outputs["Size"], grid2.inputs["Size X"])
    ng2.links.new(grid2.outputs["Mesh"], go2.inputs["Geometry"])
    o3 = base_mesh("GN2")
    mod3 = o3.modifiers.new("GN", "NODES")
    mod3.node_group = ng2
    key = [k for k in mod3.properties.inputs.keys()]
    report("modifier_baseline", measure(o3, ("obj",)))
    recaps = {}
    for tags in ((), ("obj",), ("group",)):
        mod3.properties.inputs[key[0]]["value"] = 2.0
        measure(o3, ("obj",))
        mod3.properties.inputs[key[0]]["value"] = 8.0
        recaps["+".join(tags) or "none"] = measure(o3, tags)
        mod3.properties.inputs[key[0]]["value"] = 2.0
        measure(o3, ("obj",))
    report("modifier_param_flush", recaps)

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
