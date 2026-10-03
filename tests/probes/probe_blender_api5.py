"""Fifth runtime probe: IDPropertyGroup shape of GN modifier input values."""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE5::%s::%s" % (title, json.dumps(payload, default=str)))


def main():
    ng = bpy.data.node_groups.new("ProbeGN", "GeometryNodeTree")
    a = ng.interface.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat")
    a.default_value = 1.0
    b = ng.interface.new_socket("Density", in_out="INPUT", socket_type="NodeSocketInt")
    b.default_value = 4
    c = ng.interface.new_socket("Offset", in_out="INPUT", socket_type="NodeSocketVector")
    c.default_value = (0.0, 0.0, 0.0)
    e = ng.interface.new_socket("Flag", in_out="INPUT", socket_type="NodeSocketBool")
    e.default_value = True
    f = ng.interface.new_socket("Label", in_out="INPUT", socket_type="NodeSocketString")
    f.default_value = "hi"
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    grid = ng.nodes.new("GeometryNodeMeshGrid")
    ng.links.new(grid.outputs["Mesh"], go.inputs["Geometry"])

    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("GN", "NODES")
    mod.node_group = ng

    inp = mod.properties.inputs

    def soft(label, fn):
        try:
            report(label, fn())
        except Exception as exc:  # noqa: BLE001
            report("FAIL_" + label, "%s: %s" % (type(exc).__name__, exc))

    soft("keys", lambda: list(inp.keys()))
    soft("to_dict", lambda: inp.to_dict())
    soft("item_types", lambda: {k: type(inp[k]).__name__ for k in inp.keys()})
    soft("item_dicts", lambda: {k: (inp[k].to_dict() if hasattr(inp[k], "to_dict") else repr(inp[k])) for k in inp.keys()})
    soft("item_value_attr", lambda: {k: hasattr(inp[k], "value") for k in inp.keys()})
    soft("item_keys", lambda: {k: list(inp[k].keys()) if hasattr(inp[k], "keys") else None for k in inp.keys()})

    deps = bpy.context.evaluated_depsgraph_get()

    def snap():
        deps.update()
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        xs = [round(v.co.x, 4) for v in me.vertices]
        n = len(me.vertices)
        ev.to_mesh_clear()
        return {"n": n, "max_x": max(xs) if xs else None}

    report("before", snap())
    res = {}
    try:
        inp["Socket_0"]["value"] = 8.0
        res["set_float_nested"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["set_float_nested"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    try:
        inp["Socket_1"]["value"] = 3
        res["set_int_nested"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["set_int_nested"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    try:
        inp["Socket_2"]["value"] = (1.0, 2.0, 3.0)
        res["set_vector_nested"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["set_vector_nested"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    try:
        inp["Socket_3"]["value"] = False
        res["set_bool_nested"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["set_bool_nested"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    try:
        inp["Socket_4"]["value"] = "bye"
        res["set_string_nested"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["set_string_nested"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    report("set_results", res)
    report("after", snap())
    soft("read_back", lambda: inp.to_dict())
    soft("read_back_items", lambda: {k: inp[k].to_dict() for k in inp.keys()})

    # also: does assigning the whole dict work?
    res2 = {}
    try:
        inp["Socket_0"]["value"] = 2.5
        res2["again"] = inp["Socket_0"]["value"]
    except Exception as exc:  # noqa: BLE001
        res2["again"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
    report("res2", res2)

    # interface item flags that control exposure in the modifier UI
    item = ng.interface.items_tree[1]
    report("item_flags", {n: getattr(item, n, "<missing>") for n in ("hide_value", "hide_in_modifier", "optional_label", "default_attribute_name", "force_non_field", "structure_type")})

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
