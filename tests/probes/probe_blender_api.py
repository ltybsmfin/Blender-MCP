"""Runtime probe of the Blender API surface used by the Blender MCP add-on.

Run inside Blender (never imports the ``mcp`` package):

    blender.exe --background --factory-startup \
        --python MCP/tests/probe_blender_api.py --python-exit-code 1

Prints a compact report so the implementation can rely on verified API shapes
for Geometry Nodes interfaces, shape keys, drivers, evaluated meshes and
``bpy.app.timers`` behaviour in background mode.
"""

from __future__ import annotations

import json
import sys
import traceback

import bpy


def report(title: str, payload) -> None:
    print("PROBE::%s::%s" % (title, json.dumps(payload, default=str)))


def safe(fn, label):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - probe wants the real failure
        report("FAIL_" + label, "%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        return None


def main() -> int:
    report(
        "env",
        {
            "blender": bpy.app.version_string,
            "version_tuple": list(bpy.app.version),
            "python": sys.version,
            "background": bpy.app.background,
            "factory_startup": bpy.app.factory_startup,
        },
    )

    # ---------------------------------------------------------------- geometry nodes
    ng = bpy.data.node_groups.new("ProbeGN", "GeometryNodeTree")
    report("ng_initial_nodes", [n.bl_idname for n in ng.nodes])
    report("interface_has_new_socket", hasattr(ng.interface, "new_socket"))

    def mk_interface():
        item = ng.interface.new_socket("Scale", in_out="INPUT", socket_type="NodeSocketFloat")
        item.default_value = 1.5
        item.min_value = 0.0
        item.max_value = 10.0
        item.description = "probe"
        item.subtype = "FACTOR"
        out = ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        return {
            "scale_identifier": item.identifier,
            "out_identifier": out.identifier,
            "items": [(i.name, i.identifier, i.in_out, i.socket_type) for i in ng.interface.items_tree],
        }

    report("interface", safe(mk_interface, "interface"))

    def mk_nodes():
        gi = ng.nodes.new("NodeGroupInput")
        go = ng.nodes.new("NodeGroupOutput")
        cube = ng.nodes.new("GeometryNodeMeshCube")
        tr = ng.nodes.new("GeometryNodeTransform")
        return {
            "ids": [gi.bl_idname, go.bl_idname, cube.bl_idname, tr.bl_idname],
            "cube_inputs": [
                (s.name, s.identifier, s.type, getattr(s, "default_value", "<none>")) for s in cube.inputs
            ],
            "types": sorted({s.type for s in cube.inputs}),
        }

    report("nodes", safe(mk_nodes, "nodes"))

    def do_link():
        cube = ng.nodes["Cube"]
        tr = ng.nodes["Transform"]
        out = ng.nodes["Group Output"]
        l1 = ng.links.new(cube.outputs["Mesh"], tr.inputs["Geometry"])
        l2 = ng.links.new(tr.outputs["Geometry"], out.inputs["Geometry"])
        return {
            "link_attrs": [
                a
                for a in ("from_node", "from_socket", "to_node", "to_socket", "is_valid", "is_muted")
                if hasattr(l1, a)
            ],
            "count": len(ng.links),
            "l2": bool(l2),
        }

    report("links", safe(do_link, "links"))

    def socket_search_by_identifier():
        cube = ng.nodes["Cube"]
        s = cube.inputs.get("Size")
        return {"get_by_name": s is not None, "identifiers": [x.identifier for x in cube.inputs]}

    report("socket_lookup", safe(socket_search_by_identifier, "socket_lookup"))

    # node type discovery through bpy.types
    def node_types():
        found = []
        for name in dir(bpy.types):
            if not name.startswith("GeometryNode"):
                continue
            cls = getattr(bpy.types, name)
            bl_idname = getattr(cls, "bl_idname", None)
            if bl_idname:
                found.append(bl_idname)
        rna = getattr(bpy.types.GeometryNodeTransform, "bl_rna", None)
        props = []
        if rna is not None:
            props = sorted(p.identifier for p in rna.properties)
        return {"count": len(found), "sample": sorted(found)[:8], "transform_props": props}

    report("node_types", safe(node_types, "node_types"))

    # ---------------------------------------------------------------- mesh + modifier
    mesh = bpy.data.meshes.new("ProbeMesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new("ProbeObj", mesh)
    bpy.context.scene.collection.objects.link(obj)

    def modifier():
        mod = obj.modifiers.new("GN", "NODES")
        mod.node_group = ng
        mod["Socket_2"] = 3.0
        keys = [k for k in mod.keys() if k != "_RNA_UI"]
        tr = ng.nodes["Transform"]
        tr.inputs["Translation"].default_value[0] = 2.0
        tr.inputs["Translation"].default_value = (5.0, 0.0, 0.0)
        return {
            "mod_keys": keys,
            "socket2": mod.get("Socket_2"),
            "identifier": [i.identifier for i in ng.interface.items_tree],
        }

    report("modifier", safe(modifier, "modifier"))

    def evaluated():
        deps = bpy.context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        verts = len(me.vertices)
        coords = [tuple(round(c, 4) for c in v.co) for v in me.vertices][:2]
        ev.to_mesh_clear()
        base = len(obj.data.vertices)
        return {"base_verts": base, "eval_verts": verts, "first_coords": coords}

    report("evaluated", safe(evaluated, "evaluated"))

    # ---------------------------------------------------------------- shape keys
    def shape_keys():
        obj.shape_key_add(name="Basis", from_mix=False)
        kb = obj.shape_key_add(name="Wide", from_mix=False)
        kb.data[0].co = (9.0, 0.0, 0.0)
        kb.value = 0.4
        kb.slider_min = 0.0
        kb.slider_max = 2.0
        keys = [(k.name, k.value, len(k.data)) for k in obj.data.shape_keys.key_blocks]
        return {"keys": keys, "active": obj.active_shape_key_index}

    report("shape_keys", safe(shape_keys, "shape_keys"))

    # ---------------------------------------------------------------- custom props + drivers
    obj["phase"] = 0.0

    def drivers():
        fcu = obj.driver_add("location", 2)
        drv = fcu.driver
        drv.type = "SCRIPTED"
        drv.expression = "phase * 2.0"
        var = drv.variables.new()
        var.name = "phase"
        tgt = var.targets[0]
        tgt.id_type = "OBJECT"
        tgt.id = obj
        tgt.data_path = '["phase"]'
        attrs = [a for a in ("id_type", "id", "data_path", "use_fallback_id", "fallback_id_type") if hasattr(tgt, a)]
        resolved = None
        try:
            resolved = obj.path_resolve('["phase"]', False)
        except Exception as exc:  # noqa: BLE001
            resolved = "%s: %s" % (type(exc).__name__, exc)
        bad = None
        try:
            obj.path_resolve("nope_nope", False)
            bad = "no-error"
        except Exception as exc:  # noqa: BLE001
            bad = type(exc).__name__
        return {
            "target_attrs": attrs,
            "resolved": resolved,
            "bad_path_error": bad,
            "driver_valid": drv.is_valid,
            "owner": fcu.id_data.name,
        }

    report("drivers", safe(drivers, "drivers"))

    def driver_eval():
        obj["phase"] = 1.25
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        ev = obj.evaluated_get(deps)
        return {"z_base": obj.location[2], "z_eval": ev.location[2]}

    report("driver_eval", safe(driver_eval, "driver_eval"))

    def driver_owners():
        return {
            "shape_keys_driver_add": hasattr(bpy.types.Object, "driver_add"),
            "key_driver_add": hasattr(bpy.types.Key, "driver_add"),
            "nodegroup_driver_add": hasattr(bpy.types.NodeTree, "driver_add"),
            "scene_driver_add": hasattr(bpy.types.Scene, "driver_add"),
            "key_blocks_len": len(obj.data.shape_keys.key_blocks),
        }

    report("driver_owners", safe(driver_owners, "driver_owners"))

    # ---------------------------------------------------------------- primitives / ops
    def primitive():
        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(3, 0, 0))
        new = bpy.context.active_object
        return {"name": new.name, "verts": len(new.data.vertices)}

    report("primitive", safe(primitive, "primitive"))

    # ---------------------------------------------------------------- timers
    def timers():
        ticks = []

        def tick():
            ticks.append(len(ticks))
            if len(ticks) >= 3:
                return None
            return 0.0

        bpy.app.timers.register(tick)
        return {"registered": bpy.app.timers.is_registered(tick), "note": "timer is NOT pumped in background"}

    report("timers", safe(timers, "timers"))

    def select():
        import select as _select

        return {"has_select": True, "poll": str(_select.poll)}

    report("select", safe(select, "select"))

    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
