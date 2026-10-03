"""Seventh probe: which update call actually flushes geometry-changes in background?

The MCP server must assert *evaluated* results, so it needs to know exactly how
to make Blender re-evaluate a mesh after writing an ID-property backed Geometry
Nodes parameter, a shape key value and a node socket default.
"""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE7::%s::%s" % (title, json.dumps(payload, default=str)))


def stats(obj, update=None):
    if update == "deps":
        bpy.context.evaluated_depsgraph_get().update()
    elif update == "viewlayer":
        bpy.context.view_layer.update()
    elif update == "both":
        bpy.context.view_layer.update()
        bpy.context.evaluated_depsgraph_get().update()
    elif update == "tagdeps":
        obj.update_tag()
        bpy.context.evaluated_depsgraph_get().update()
    deps = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(deps)
    mesh = ev.to_mesh()
    try:
        xs = [v.co.x for v in mesh.vertices]
        zs = [v.co.z for v in mesh.vertices]
        return {"n": len(mesh.vertices), "min_x": round(min(xs), 4), "max_x": round(max(xs), 4),
                "max_z": round(max(zs), 4)}
    finally:
        ev.to_mesh_clear()


def main():
    ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
    size = ng.interface.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat")
    size.default_value = 2.0
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    grid = ng.nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Vertices X"].default_value = 4
    grid.inputs["Vertices Y"].default_value = 4
    ng.links.new(ng.nodes["Group Input"].outputs["Size"], grid.inputs["Size X"])
    ng.links.new(ng.nodes["Group Input"].outputs["Size"], grid.inputs["Size Y"])
    ng.links.new(grid.outputs["Mesh"], go.inputs["Geometry"])

    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("GN", "NODES")
    mod.node_group = ng

    report("baseline", stats(obj))
    report("interface_ids", [(i.name, i.identifier) for i in ng.interface.items_tree])
    key = [k for k in mod.properties.inputs.keys()][0]

    # --- modifier parameter, one strategy per fresh attempt --------------- #
    scenarios = []
    for strategy in ("none", "deps", "viewlayer", "both", "tagdeps"):
        mod.properties.inputs[key]["value"] = 6.0
        result = stats(obj, None if strategy == "none" else strategy)
        scenarios.append({"strategy": strategy, "value": 6.0, "result": result})
        # reset for the next scenario
        mod.properties.inputs[key]["value"] = 2.0
        stats(obj, "both")
        report("after_reset", stats(obj))
    report("modifier_param_strategies", scenarios)

    # --- direct interface default (the value the modifier instances inherit) #
    ng.interface.items_tree[1].default_value = 8.0 if hasattr(ng.interface.items_tree[1], "default_value") else None
    report("interface_default_route", {"note": "modifier instance value wins over the group default"})

    # --- node socket default (unlinked) ---------------------------------- #
    seq = []
    for strategy in ("none", "deps", "viewlayer", "both"):
        grid.inputs["Vertices Y"].default_value = 2
        result = stats(obj, None if strategy == "none" else strategy)
        seq.append({"strategy": strategy, "result": result})
        grid.inputs["Vertices Y"].default_value = 4
        stats(obj, "both")
    report("node_socket_default_strategies", seq)

    # --- shape key value ------------------------------------------------- #
    obj.shape_key_add(name="Basis")
    kb = obj.shape_key_add(name="Wide")
    for i in range(len(kb.data)):
        kb.data[i].co = (kb.data[i].co.x * 2.0, 0.0, 0.0)
    seq = []
    for strategy in ("none", "deps", "viewlayer", "both"):
        kb.value = 1.0
        result = stats(obj, None if strategy == "none" else strategy)
        seq.append({"strategy": strategy, "result": result})
        kb.value = 0.0
        stats(obj, "both")
    report("shape_key_strategies", seq)

    # --- does the modifier parameter survive a real frame change? -------- #
    mod.properties.inputs[key]["value"] = 10.0
    bpy.context.scene.frame_set(3)
    report("after_frame_set", stats(obj))
    mod.properties.inputs[key]["value"] = 2.0
    bpy.context.scene.frame_set(4)
    report("after_frame_set_reset", stats(obj))

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
