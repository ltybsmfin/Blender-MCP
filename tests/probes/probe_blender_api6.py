"""Sixth probe: shape key Basis semantics and the 5.x Action/F-Curve API."""

from __future__ import annotations

import json

import bpy


def report(title, payload):
    print("PROBE6::%s::%s" % (title, json.dumps(payload, default=str)))


def main():
    # ---- shape key basis semantics ------------------------------------- #
    mesh = bpy.data.meshes.new("M")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("O", mesh)
    bpy.context.scene.collection.objects.link(obj)

    report("no_keys_initially", {"shape_keys": obj.data.shape_keys is not None})
    first = obj.shape_key_add(name="ScaleX", from_mix=False) if False else None
    report("before_any_key", {"has": obj.data.shape_keys is not None})

    basis = obj.shape_key_add(name="Basis", from_mix=False)
    report(
        "after_basis",
        {
            "keys": [(k.name, k.value, k.relative_key.name) for k in obj.data.shape_keys.key_blocks],
            "basis_is_index0": obj.data.shape_keys.key_blocks[0].name == "Basis",
        },
    )
    second = obj.shape_key_add(name="ScaleX", from_mix=False)
    report(
        "after_second",
        {
            "keys": [(k.name, k.value, k.relative_key.name) for k in obj.data.shape_keys.key_blocks],
            "basis_verts": len(obj.data.shape_keys.key_blocks["Basis"].data),
            "second_verts": len(second.data),
            "active_index": obj.active_shape_key_index,
        },
    )

    # a key with no basis (what happens if you name the first key something else)
    mesh2 = bpy.data.meshes.new("M2")
    mesh2.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh2.update()
    obj2 = bpy.data.objects.new("O2", mesh2)
    bpy.context.scene.collection.objects.link(obj2)
    only = obj2.shape_key_add(name="Direct", from_mix=False)
    report(
        "single_non_basis_named_key",
        {
            "keys": [(k.name, k.relative_key.name) for k in obj2.data.shape_keys.key_blocks],
            "index": list(obj2.data.shape_keys.key_blocks).index(only),
            "is_index0": obj2.data.shape_keys.key_blocks[0] is only,
        },
    )

    # ---- action / fcurve API ------------------------------------------- #
    obj.location[0] = 0.0
    obj.keyframe_insert(data_path="location", index=0, frame=1)
    action = obj.animation_data.action
    report(
        "action_api",
        {
            "action": action.name,
            "has_fcurves": hasattr(action, "fcurves"),
            "attrs": [a for a in dir(action) if not a.startswith("_")],
        },
    )
    layers = getattr(action, "layers", None)
    if layers is not None:
        report("layers_len", len(layers))
        for i, layer in enumerate(layers):
            report("layer_%d_attrs" % i, [a for a in dir(layer) if not a.startswith("_")])
            for j, strip in enumerate(layer.strips):
                report("strip_%d_attrs" % (i, j) if False else "strip_attrs", [a for a in dir(strip) if not a.startswith("_")])
                bags = getattr(strip, "channelbags", None)
                report("strip_channelbags", {"has": bags is not None, "len": len(bags) if bags is not None else None})
                if bags is not None:
                    for bag in bags:
                        report("bag_fcurves", [f.data_path for f in bag.fcurves])

    def collect(action_obj):
        if hasattr(action_obj, "fcurves"):
            return [f.data_path for f in action_obj.fcurves]
        out = []
        for layer in getattr(action_obj, "layers", []):
            for strip in layer.strips:
                bags = getattr(strip, "channelbags", None)
                if bags is None:
                    continue
                for bag in bags:
                    out.extend(f.data_path for f in bag.fcurves)
        return out

    report("collect_helper", collect(action))

    # ---- drivers still on animation_data? ------------------------------ #
    obj["phase"] = 0.0
    fcu = obj.driver_add("rotation_euler", 2)
    fcu.driver.expression = "phase"
    var = fcu.driver.variables.new()
    var.name = "phase"
    var.targets[0].id_type = "OBJECT"
    var.targets[0].id = obj
    var.targets[0].data_path = '["phase"]'
    report(
        "drivers_collection",
        {
            "count": len(obj.animation_data.drivers),
            "paths": [f.data_path for f in obj.animation_data.drivers],
            "indices": [f.array_index for f in obj.animation_data.drivers],
            "action_is_none": obj.animation_data.action is None,
            "driver_is_valid": fcu.driver.is_valid,
        },
    )
    obj["phase"] = 2.0
    bpy.context.evaluated_depsgraph_get().update()
    report("driver_after_update", {"rotation_z": obj.rotation_euler[2]})
    report("fcurves_still_there", collect(obj.animation_data.action) if obj.animation_data.action else None)
    report(
        "driver_remove_api",
        {"has_driver_remove": hasattr(obj, "driver_remove")},
    )
    report("ok", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
