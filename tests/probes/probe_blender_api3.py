"""Third runtime probe: GN modifier input API (5.2), node type discovery, driver paths."""

from __future__ import annotations

import json
import traceback

import bpy


def report(title, payload):
    print("PROBE3::%s::%s" % (title, json.dumps(payload, default=str)))


def safe(fn, label):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        report("FAIL_" + label, "%s: %s" % (type(exc).__name__, exc))
        return None


def main():
    ng = bpy.data.node_groups.new("ProbeGN", "GeometryNodeTree")
    ng.interface.new_socket("Scale", in_out="INPUT", socket_type="NodeSocketFloat").default_value = 1.0
    ng.interface.new_socket("Offset", in_out="INPUT", socket_type="NodeSocketVector")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    cube = ng.nodes.new("GeometryNodeMeshCube")
    tr = ng.nodes.new("GeometryNodeTransform")
    tr.name = "XForm"
    ng.links.new(cube.outputs["Mesh"], tr.inputs["Geometry"])
    ng.links.new(tr.outputs["Geometry"], go.inputs["Geometry"])

    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mod = obj.modifiers.new("GN", "NODES")
    mod.node_group = ng

    def props_rna():
        p = mod.properties
        return {
            "type": type(p).__name__,
            "bl_rna_id": p.bl_rna.identifier,
            "rna_props": sorted(x.identifier for x in p.bl_rna.properties),
        }

    report("mod_properties", safe(props_rna, "mod_properties"))

    def props_dir():
        p = mod.properties
        names = [n for n in dir(p) if not n.startswith("_")]
        out = {}
        for n in names:
            try:
                out[n] = repr(getattr(p, n))[:120]
            except Exception as exc:  # noqa: BLE001
                out[n] = "ERR %s" % type(exc).__name__
        return out

    report("mod_properties_dir", safe(props_dir, "mod_properties_dir"))

    def try_many():
        out = {}
        attempts = {
            "properties.inputs": lambda: mod.properties.inputs,
            "properties.inputs['Scale']": lambda: mod.properties.inputs["Scale"],
            "properties['Scale']": lambda: mod.properties["Scale"],
            "properties.inputs['Scale'].value": lambda: mod.properties.inputs["Scale"].value,
            "properties.inputs['Scale'].default_value": lambda: mod.properties.inputs["Scale"].default_value,
        }
        for label, fn in attempts.items():
            try:
                out[label] = repr(fn())[:160]
            except Exception as exc:  # noqa: BLE001
                out[label] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return out

    report("try_many", safe(try_many, "try_many"))

    def set_and_check():
        """Try to set a value and confirm it changes the evaluated geometry."""
        results = {}
        deps = bpy.context.evaluated_depsgraph_get()

        def eval_max_x():
            deps.update()
            ev = obj.evaluated_get(deps)
            me = ev.to_mesh()
            xs = [v.co.x for v in me.vertices] or [0.0]
            n = len(me.vertices)
            ev.to_mesh_clear()
            return {"n": n, "max_x": max(xs)}

        results["before"] = eval_max_x()
        for label, fn in (
            ("properties.inputs['Offset'].value", lambda: setattr(mod.properties.inputs["Offset"], "value", (5.0, 0, 0))),
            ("properties.inputs['Scale'].value", lambda: setattr(mod.properties.inputs["Scale"], "value", 2.0)),
        ):
            try:
                fn()
                results[label] = "ok"
            except Exception as exc:  # noqa: BLE001
                results[label] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        results["after"] = eval_max_x()
        return results

    report("set_and_check", safe(set_and_check, "set_and_check"))

    def interface_socket_attrs():
        item = ng.interface.items_tree[0]
        return {
            "attrs": [a for a in dir(item) if not a.startswith("_")],
            "identifier": item.identifier,
            "socket_type": item.socket_type,
            "default_value": repr(getattr(item, "default_value", None)),
            "is_input": item.in_out,
        }

    report("interface_socket_attrs", safe(interface_socket_attrs, "interface_socket_attrs"))

    # ---------- node type discovery
    def discovery():
        out = {}

        def strat(name, fn):
            try:
                out[name] = fn()
            except Exception as exc:  # noqa: BLE001
                out[name] = "%s: %s" % (type(exc).__name__, exc)

        strat("GeometryNode_subclasses", lambda: len(bpy.types.GeometryNode.__subclasses__()))
        strat("GeometryNode_samples", lambda: sorted(c.__name__ for c in bpy.types.GeometryNode.__subclasses__())[:6])
        strat("dir_bpy_types_gn", lambda: len([n for n in dir(bpy.types) if n.startswith("GeometryNode")]))
        strat("dir_bpy_types_shader", lambda: len([n for n in dir(bpy.types) if n.startswith("ShaderNode")]))
        strat("dir_bpy_types_function", lambda: len([n for n in dir(bpy.types) if n.startswith("FunctionNode")]))
        strat("dir_bpy_types_compositor", lambda: len([n for n in dir(bpy.types) if n.startswith("CompositorNode")]))
        strat("dir_bpy_types_all_nodes", lambda: len([n for n in dir(bpy.types) if n.endswith("Node")]))
        return out

    report("discovery", safe(discovery, "discovery"))

    def rna_of_type():
        cls = bpy.types.GeometryNodeTransform
        return {
            "identifier": cls.bl_rna.identifier,
            "props": sorted(p.identifier for p in cls.bl_rna.properties),
            "has_bl_idname_prop": "bl_idname" in [p.identifier for p in cls.bl_rna.properties],
        }

    report("rna_of_type", safe(rna_of_type, "rna_of_type"))

    # ---------- drivers
    def drivers():
        out = {}
        # node group socket default, correct node name
        for label, path, index in (
            ("by_name", 'nodes["XForm"].inputs["Translation"].default_value', 0),
            ("by_index", 'nodes["XForm"].inputs[2].default_value', 0),
            ("path_resolve_by_name", None, None),
        ):
            if path is None:
                try:
                    out[label] = str(ng.path_resolve('nodes["XForm"].inputs["Translation"].default_value', False))
                except Exception as exc:  # noqa: BLE001
                    out[label] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                continue
            try:
                fcu = ng.driver_add(path, index)
                fcu.driver.expression = "3.0"
                out[label] = "ok valid=%s value=%s" % (
                    fcu.driver.is_valid,
                    tuple(ng.nodes["XForm"].inputs["Translation"].default_value),
                )
            except Exception as exc:  # noqa: BLE001
                out[label] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
        # scene custom property
        sc = bpy.context.scene
        sc["amp"] = 3.0
        try:
            fcu = sc.driver_add('["amp"]')
            fcu.driver.expression = "7.0"
            deps = bpy.context.evaluated_depsgraph_get()
            deps.update()
            out["scene_custom"] = "ok value=%s" % sc["amp"]
        except Exception as exc:  # noqa: BLE001
            out["scene_custom"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
        # object custom property driver
        obj["phase"] = 0.0
        try:
            fcu = obj.driver_add('["phase"]')
            fcu.driver.expression = "4.0"
            deps = bpy.context.evaluated_depsgraph_get()
            deps.update()
            out["object_custom"] = "ok value=%s" % obj["phase"]
        except Exception as exc:  # noqa: BLE001
            out["object_custom"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
        # driver on object rotation via custom property target
        fcu = obj.driver_add("rotation_euler", 2)
        fcu.driver.expression = "phase * 2"
        var = fcu.driver.variables.new()
        var.name = "phase"
        var.targets[0].id_type = "OBJECT"
        var.targets[0].id = obj
        var.targets[0].data_path = '["phase"]'
        obj["phase"] = 1.5
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        out["rotation_z"] = obj.rotation_euler[2]
        out["rotation_valid"] = fcu.driver.is_valid
        return out

    report("drivers", safe(drivers, "drivers"))

    def bounds_now():
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        ev = obj.evaluated_get(deps)
        return {"rot_z_eval": ev.rotation_euler[2], "rot_z_base": obj.rotation_euler[2]}

    report("bounds_now", safe(bounds_now, "bounds_now"))

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
