"""Second runtime probe: node naming, modifier inputs, node-type discovery.

    blender.exe --background --factory-startup --python-exit-code 1 \
        --python MCP/tests/probe_blender_api2.py
"""

from __future__ import annotations

import json
import traceback

import bpy


def report(title, payload):
    print("PROBE2::%s::%s" % (title, json.dumps(payload, default=str)))


def safe(fn, label):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        report("FAIL_" + label, "%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        return None


def main():
    ng = bpy.data.node_groups.new("ProbeGN", "GeometryNodeTree")
    ng.interface.new_socket("Scale", in_out="INPUT", socket_type="NodeSocketFloat").default_value = 1.0
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    tr = ng.nodes.new("GeometryNodeTransform")
    report("node_names", [n.name for n in ng.nodes])
    report("node_labels", [n.label for n in ng.nodes])
    report("node_props", sorted(p.identifier for p in tr.bl_rna.properties))

    ng.links.new(cube.outputs["Mesh"], tr.inputs["Geometry"])
    ng.links.new(tr.outputs["Geometry"], go.inputs["Geometry"])
    report("links", [(l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier) for l in ng.links])

    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("GN", "NODES")
    mod.node_group = ng
    report("mod_props", sorted(p.identifier for p in mod.bl_rna.properties))
    report("mod_has_inputs", hasattr(mod, "inputs"))
    if hasattr(mod, "inputs"):
        report("mod_inputs", [(getattr(s, "name", None), getattr(s, "identifier", None), getattr(s, "type", None)) for s in mod.inputs])

    def try_set():
        out = {}
        for label, fn in (
            ("mod[socket_id]", lambda: mod.__setitem__("Socket_0", 3.0)),
            ("mod.properties", lambda: setattr(mod, "properties", {})),
        ):
            try:
                fn()
                out[label] = "ok"
            except Exception as exc:  # noqa: BLE001
                out[label] = "%s: %s" % (type(exc).__name__, exc)
        return out

    report("mod_set", safe(try_set, "mod_set"))

    def nodes_modifier_rna():
        return {
            "NodesModifier": hasattr(bpy.types, "NodesModifier"),
            "props": sorted(p.identifier for p in bpy.types.NodesModifier.bl_rna.properties)
            if hasattr(bpy.types, "NodesModifier")
            else [],
        }

    report("nodes_modifier_rna", safe(nodes_modifier_rna, "nodes_modifier_rna"))

    # ---- node type discovery strategies
    def discover():
        res = {}

        def strat(name, fn):
            try:
                v = fn()
                res[name] = v
            except Exception as exc:  # noqa: BLE001
                res[name] = "%s: %s" % (type(exc).__name__, exc)

        strat(
            "bl_idname_attr",
            lambda: bpy.types.GeometryNodeTransform.bl_idname,
        )
        strat(
            "rna_identifier",
            lambda: bpy.types.GeometryNodeTransform.bl_rna.identifier,
        )
        strat(
            "bl_idname_rna_default",
            lambda: bpy.types.GeometryNodeTransform.bl_rna.properties["bl_idname"].default,
        )
        strat(
            "node_subclasses_count",
            lambda: len(bpy.types.Node.__subclasses__()),
        )
        strat(
            "node_subclasses_sample",
            lambda: sorted(c.__name__ for c in bpy.types.Node.__subclasses__())[:10],
        )
        strat(
            "nodeinternal_subclasses_gn",
            lambda: sorted(c.__name__ for c in bpy.types.NodeInternal.__subclasses__() if c.__name__.startswith("GeometryNode"))[:10],
        )
        strat(
            "dir_bpy_types_contains",
            lambda: "GeometryNodeTransform" in dir(bpy.types),
        )
        strat(
            "bl_rna_types_of_nodegroups",
            lambda: len(bpy.types.Node.bl_rna.functions),
        )
        return res

    report("discover", safe(discover, "discover"))

    # ---- driver on node group socket default & shape keys
    def ng_driver():
        fcu = ng.driver_add('nodes["Transform"].inputs["Translation"].default_value', 0)
        fcu.driver.type = "SCRIPTED"
        fcu.driver.expression = "2.0"
        return {"ok": True, "valid": fcu.driver.is_valid}

    report("ng_driver", safe(ng_driver, "ng_driver"))

    def sk_drivers():
        obj.shape_key_add(name="Basis")
        kb = obj.shape_key_add(name="Wide")
        sk = obj.data.shape_keys
        fcu = sk.driver_add('key_blocks["Wide"].value')
        fcu.driver.expression = "0.75"
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        return {
            "value": obj.data.shape_keys.key_blocks["Wide"].value,
            "valid": fcu.driver.is_valid,
            "path_resolve": str(sk.path_resolve('key_blocks["Wide"].value', False)),
        }

    report("sk_drivers", safe(sk_drivers, "sk_drivers"))

    def scene_custom():
        sc = bpy.context.scene
        sc["amp"] = 3
        fcu = sc.driver_add("frame_start")
        fcu.driver.expression = "amp"
        var = fcu.driver.variables.new()
        var.name = "amp"
        var.targets[0].id_type = "SCENE"
        var.targets[0].id = sc
        var.targets[0].data_path = '["amp"]'
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        return {"frame_start": sc.frame_start, "valid": fcu.driver.is_valid}

    report("scene_custom", safe(scene_custom, "scene_custom"))

    # ---- evaluated bounds
    def bounds():
        deps = bpy.context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        n = len(me.vertices)
        xs = [v.co.x for v in me.vertices] or [0.0]
        ev.to_mesh_clear()
        return {"verts": n, "max_x": max(xs), "bound_box": [list(b) for b in ev.bound_box]}

    report("bounds", safe(bounds, "bounds"))

    def socket_types():
        return sorted({s.type for s in ng.nodes["Group Output"].inputs})

    report("socket_types", safe(socket_types, "socket_types"))

    def socket_compat():
        """Which socket type names exist for interface sockets."""
        names = []
        for st in ("NodeSocketFloat", "NodeSocketInt", "NodeSocketBool", "NodeSocketGeometry",
                   "NodeSocketVector", "NodeSocketString", "NodeSocketColor", "NodeSocketMenu",
                   "NodeSocketMaterial", "NodeSocketObject", "NodeSocketCollection", "NodeSocketImage",
                   "NodeSocketRotation"):
            try:
                ng.interface.new_socket("t_" + st, in_out="INPUT", socket_type=st)
                names.append(st)
            except Exception as exc:  # noqa: BLE001
                names.append("%s -> %s" % (st, type(exc).__name__))
        return names

    report("interface_socket_types", safe(socket_compat, "socket_compat"))

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
