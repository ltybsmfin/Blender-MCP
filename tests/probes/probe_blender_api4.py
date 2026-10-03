"""Fourth runtime probe: exact 5.2 GN modifier input (per-instance) API."""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE4::%s::%s" % (title, json.dumps(payload, default=str)))


def main():
    ng = bpy.data.node_groups.new("ProbeGN", "GeometryNodeTree")
    s = ng.interface.new_socket("Size", in_out="INPUT", socket_type="NodeSocketFloat")
    s.default_value = 1.0
    s.min_value = 0.1
    s.max_value = 10.0
    d = ng.interface.new_socket("Density", in_out="INPUT", socket_type="NodeSocketInt")
    d.default_value = 4
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    grid = ng.nodes.new("GeometryNodeMeshGrid")
    ng.links.new(grid.outputs["Mesh"], go.inputs["Geometry"])

    report(
        "interface_items",
        [
            (i.name, i.identifier, i.in_out, i.socket_type, repr(getattr(i, "default_value", None)))
            for i in ng.interface.items_tree
        ],
    )

    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("GN", "NODES")
    mod.node_group = ng

    def inputs_rna():
        inputs = mod.properties.inputs
        return {
            "type": type(inputs).__name__,
            "bl_rna_props": sorted(p.identifier for p in inputs.bl_rna.properties),
            "len": len(inputs),
            "keys": [k for k in inputs.keys()],
            "items": [
                {
                    "name": getattr(i, "name", None),
                    "identifier": getattr(i, "identifier", None),
                    "attrs": [a for a in dir(i) if not a.startswith("_")],
                }
                for i in inputs
            ],
        }

    try:
        report("inputs_rna", inputs_rna())
    except Exception as exc:  # noqa: BLE001
        report("FAIL_inputs_rna", "%s: %s" % (type(exc).__name__, exc))

    def set_by_identifier():
        out = {}
        inp = mod.properties.inputs
        for key in list(inp.keys()):
            item = inp[key]
            for attr in ("value", "default_value"):
                if hasattr(item, attr):
                    out.setdefault("attr", attr)
        return out

    try:
        report("set_by_identifier", set_by_identifier())
    except Exception as exc:  # noqa: BLE001
        report("FAIL_set_by_identifier", "%s: %s" % (type(exc).__name__, exc))

    def apply_value():
        deps = bpy.context.evaluated_depsgraph_get()

        def snapshot():
            deps.update()
            ev = obj.evaluated_get(deps)
            me = ev.to_mesh()
            xs = [round(v.co.x, 4) for v in me.vertices]
            n = len(me.vertices)
            ev.to_mesh_clear()
            return {"n": n, "max_x": max(xs) if xs else None}

        before = snapshot()
        inp = mod.properties.inputs
        keys = list(inp.keys())
        res = {"keys": keys, "before": before}
        size_item = inp[keys[0]]
        attr = "value" if hasattr(size_item, "value") else "default_value"
        try:
            setattr(size_item, attr, 6.0)
            res["set_size"] = "ok(%s)" % attr
        except Exception as exc:  # noqa: BLE001
            res["set_size"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        if len(keys) > 1:
            density_item = inp[keys[1]]
            try:
                setattr(density_item, attr, 3)
                res["set_density"] = "ok"
            except Exception as exc:  # noqa: BLE001
                res["set_density"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        res["after"] = snapshot()
        res["read_back"] = [(k, repr(getattr(inp[k], attr, None))) for k in keys]
        return res

    try:
        report("apply_value", apply_value())
    except Exception as exc:  # noqa: BLE001
        report("FAIL_apply_value", "%s: %s" % (type(exc).__name__, exc))

    def default_value_fallback():
        """If per-instance inputs are unusable, group interface defaults still work."""
        return {
            "interface_default": ng.interface.items_tree[0].default_value
            if hasattr(ng.interface.items_tree[0], "default_value")
            else None
        }

    try:
        report("default_value_fallback", default_value_fallback())
    except Exception as exc:  # noqa: BLE001
        report("FAIL_default_value_fallback", "%s: %s" % (type(exc).__name__, exc))

    def modifier_apply_ops():
        return {"has_apply": hasattr(bpy.ops.object, "modifier_apply")}

    try:
        report("modifier_apply_ops", modifier_apply_ops())
    except Exception as exc:  # noqa: BLE001
        report("FAIL_modifier_apply_ops", "%s: %s" % (type(exc).__name__, exc))

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
