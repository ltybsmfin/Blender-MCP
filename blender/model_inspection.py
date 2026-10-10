"""Evaluated-model inspection: **target-scene** depsgraph facts + real viewport capture.

Bridge-side command module, registered by ``blender_mcp_addon.py`` like the other
optional modules::

    import model_inspection
    model_inspection.register_commands(sys.modules[__name__])

Commands
--------
``model_inspect(object, scene=None, modifier=None)``
    Read-only.  Evaluates exactly one object in exactly one scene (never silently
    in the active scene) and reports what the Blender API can really prove:
    evaluated geometry component types, point/edge/face/curve/instance counts,
    world-space bounds, a comparable signature, the modifier stack and explicit
    ``warnings``/``unknowns`` for everything that could not be read.  A partial
    modifier-stack evaluation is *not* offered because Blender can only provide
    it by modifying the object; ``evaluated`` therefore always describes the whole
    stack and says so.
``viewport_capture_prepare`` / ``_status`` / ``_commit`` / ``_abort``
    Capture the *real* 3D viewport area with ``bpy.ops.screen.screenshot_area``.
    The prepare step may temporarily switch the window's scene and the view, and
    both are restored by commit/abort (also when the capture fails).  There is no
    camera render, no OpenGL still-render and no cached-image fallback: without an
    observed draw of the requested scene the commit fails with ``stale_frame``.

Safety invariants
-----------------
* The object's mesh data, modifier stack and transform are never modified, the
  ``.blend`` is never saved, and every temporary mesh from ``to_mesh()`` is
  released with ``to_mesh_clear()`` in a ``finally`` block - also on error.
* Legacy statistics (``evaluated_mesh_stats``/``mesh_signature``) are reused but
  are always called with the **target scene's** depsgraph, never with
  ``bpy.context.evaluated_depsgraph_get()``.
"""

from __future__ import annotations

import hashlib
import time

import bpy

try:  # registered as a top-level module by the add-on
    from . import review_capture_common as common
except ImportError:  # pragma: no cover - the add-on path
    import review_capture_common as common

#: Bounded work so one command can never stall on a huge graph.
MAX_EXACT_POINTS = 400_000
MAX_MODIFIERS = 64
MAX_SPLINES = 64
MAX_WARNING_REFS = 20
#: Fit modes accepted by the viewport capture.
FIT_VALUES = (True, False)


def _numpy():
    try:  # Blender bundles numpy; the fallback is plain Python
        import numpy  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return numpy


def register_commands(api):
    error = api.CommandError
    numpy = _numpy()

    def fail(code, message, details=None):
        raise error(code, message, details=details)

    # ----------------------------------------------------------------- #
    # object / scene / depsgraph resolution
    # ----------------------------------------------------------------- #
    def exact_object(name):
        if not isinstance(name, str) or not name.strip():
            fail("invalid_argument", "object must be a non-empty exact object name")
        obj = bpy.data.objects.get(name)
        if obj is None:
            fail("object_not_found", "no object named %r" % name,
                 details={"available": [item.name for item in bpy.data.objects][:50]})
        return obj

    def scene_scope(obj, scene_name):
        holders = [scene for scene in bpy.data.scenes if obj.name in scene.objects]
        if scene_name is not None:
            if not isinstance(scene_name, str) or not scene_name.strip():
                fail("invalid_argument", "scene must be a non-empty exact scene name")
            scene = bpy.data.scenes.get(scene_name)
            if scene is None:
                fail("scene_not_found", "no scene named %r" % scene_name,
                     details={"available": [item.name for item in bpy.data.scenes][:50]})
            if obj.name not in scene.objects:
                fail("object_not_in_scene",
                     "object %r is not part of scene %r" % (obj.name, scene.name),
                     details={"scenes_with_object": [item.name for item in holders]})
            return scene, holders
        if len(holders) == 1:
            return holders[0], holders
        if not holders:
            fail("object_not_in_scene",
                 "object %r is not linked into any scene, so it cannot be evaluated" % obj.name,
                 details={"scenes": [item.name for item in bpy.data.scenes][:50]})
        fail("ambiguous_scene",
             "object %r is linked into %d scenes (%s); pass scene=<name> so the evaluation is exact"
             % (obj.name, len(holders), ", ".join(item.name for item in holders)),
             details={"scenes": [item.name for item in holders]})
        return None, holders

    def depsgraph_for(scene):
        layers = list(scene.view_layers)
        if not layers:
            fail("view_layer_unavailable", "the target scene has no view layer")
        layer = (bpy.context.view_layer if getattr(bpy.context, "scene", None) is scene
                 else layers[0])
        # Scene has no evaluated_depsgraph_get in 5.2. Context overrides are
        # scoped and leave the window's scene and view layer unchanged.
        try:
            with bpy.context.temp_override(scene=scene, view_layer=layer):
                deps = bpy.context.evaluated_depsgraph_get()
            deps.update()
        except Exception as exc:
            fail("evaluation_failed", "target scene dependency graph failed: %s" % exc)
        owner = getattr(deps, "scene", None)
        owner = getattr(owner, "original", owner)
        if owner is not scene:
            fail("depsgraph_scene_mismatch", "refusing another scene's evaluated geometry")
        return deps, {"source": "Context.evaluated_depsgraph_get with exact scene/view_layer override",
                      "scene_api": False, "view_layer": layer.name}

    # ----------------------------------------------------------------- #
    # geometry reading
    # ----------------------------------------------------------------- #
    def _matrix_rows(matrix):
        try:
            return [[round(float(value), 6) for value in row] for row in matrix]
        except Exception:  # noqa: BLE001
            return None

    def _bounds(points):
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        zs = [point[2] for point in points]
        return {
            "min": [round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
            "max": [round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)],
            "size": [round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6), round(max(zs) - min(zs), 6)],
            "center": [round((min(xs) + max(xs)) / 2.0, 6), round((min(ys) + max(ys)) / 2.0, 6),
                       round((min(zs) + max(zs)) / 2.0, 6)],
            "points": len(points),
        }

    def _merge_bounds(parts):
        merged = [part for part in parts if part]
        if not merged:
            return None
        return {
            "min": [min(part["min"][axis] for part in merged) for axis in range(3)],
            "max": [max(part["max"][axis] for part in merged) for axis in range(3)],
            "size": [round(max(part["max"][axis] for part in merged)
                           - min(part["min"][axis] for part in merged), 6) for axis in range(3)],
            "center": [round((min(part["min"][axis] for part in merged)
                              + max(part["max"][axis] for part in merged)) / 2.0, 6) for axis in range(3)],
            "sources": [part.get("source", "unknown") for part in merged],
        }

    def world_points(vertices, matrix, collection="co"):
        """World-space points, exactly and quickly (numpy when Blender ships it)."""
        count = len(vertices)
        if count == 0:
            return [], {"algorithm": "none", "exact": True, "truncated": 0}
        if numpy is not None and count > 8192:
            buffer = numpy.empty(count * 3, dtype="f8")
            vertices.foreach_get(collection, buffer)
            buffer = buffer.reshape(-1, 3)
            transform = numpy.array([[matrix[row][column] for column in range(4)] for row in range(4)])
            rotated = buffer @ transform[:3, :3].T
            translated = rotated + transform[:3, 3]
            points = [tuple(float(value) for value in row) for row in translated]
            return points, {"algorithm": "numpy.foreach_get", "exact": True, "truncated": 0}
        if count > MAX_EXACT_POINTS:
            step = max(1, count // MAX_EXACT_POINTS)
            subset = range(0, count, step)
            points = [tuple(matrix @ vertices[index].co) for index in subset]
            return points, {"algorithm": "python stride sample", "exact": False,
                            "truncated": count - len(points), "stride": step}
        return [tuple(matrix @ vertex.co) for vertex in vertices], {
            "algorithm": "python loop", "exact": True, "truncated": 0}

    def signature_sha256(points):
        rounded = sorted(tuple(round(float(value), 5) for value in point) for point in points)
        digest = hashlib.sha256(repr(rounded).encode("utf-8")).hexdigest()
        return digest, len(rounded)

    def curve_report(curve, matrix):
        report = {"splines": len(curve.splines), "total_points": 0, "types": {}, "points": [],
                  "bounds": None, "bounds_source": "curve control points in world space "
                                                   "(not the tessellated surface)"}
        points = []
        for index, spline in enumerate(curve.splines):
            kind = getattr(spline, "type", None)
            report["types"][kind] = int(report["types"].get(kind, 0)) + 1
            try:
                count = len(spline.points) if kind == "NURBS" or kind == "POLY" else len(spline.bezier_points)
            except Exception:  # noqa: BLE001
                count = 0
            report["total_points"] += count
            if index < MAX_SPLINES:
                report["points"].append({"index": index, "type": kind, "points": count,
                                         "cyclic": bool(getattr(spline, "use_cyclic_u", False)),
                                         "resolution": getattr(spline, "resolution_u", None)})
            try:
                if kind in ("NURBS", "POLY"):
                    points.extend(tuple(matrix @ point.co) for point in spline.points)
                else:
                    points.extend(tuple(matrix @ point.co)
                                  for point in spline.bezier_points)
            except Exception:  # noqa: BLE001
                continue
        report["bounds"] = _bounds(points)
        return report

    def instance_report(deps, obj):
        report = {"rows_with_parent": 0, "instances": 0, "sources": {}, "points_gathered": 0,
                  "truncated": False, "bounds": None, "errors": [], "bounds_unavailable_sources": []}
        points = []
        try:
            # RNA instance entries are valid only during their iterator step.
            # Materializing the iterator keeps invalid StructRNA wrappers.
            rows = iter(deps.object_instances)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append("depsgraph.object_instances unreadable: %s" % exc)
            return report
        for instance in rows:
            parent = getattr(instance, "parent", None)
            if parent is None or getattr(parent, "name", None) != obj.name:
                continue
            report["rows_with_parent"] += 1
            if not getattr(instance, "is_instance", True):
                continue
            report["instances"] += 1
            source = getattr(instance, "instance_object", None) or getattr(instance, "object", None)
            name = getattr(source, "name", None)
            report["sources"][name] = int(report["sources"].get(name, 0)) + 1
            if name is None or getattr(source, "type", None) != "MESH":
                report["bounds_unavailable_sources"].append(name)
                continue
            try:
                evaluated = source.evaluated_get(deps)
                mesh = evaluated.to_mesh()
            except Exception as exc:  # noqa: BLE001
                report["errors"].append("instance %s: %s" % (name, exc))
                continue
            try:
                matrix = instance.matrix_world
                for vertex in mesh.vertices:
                    points.append(tuple(matrix @ vertex.co))
                    if len(points) >= common.MAX_INSTANCE_POINTS:
                        report["truncated"] = True
                        break
            finally:
                evaluated.to_mesh_clear()
            if report["truncated"]:
                break
        report["points_gathered"] = len(points)
        report["bounds"] = _bounds(points)
        if report["bounds"]:
            report["bounds"]["source"] = "instance points"
        report["complete"] = (not report["truncated"] and not report["errors"]
                              and not report["bounds_unavailable_sources"])
        return report

    def legacy_stats(obj, deps, vertex_count=None):
        out = {"depsgraph": "target scene (passed explicitly)"}
        too_large = vertex_count is not None and vertex_count > MAX_EXACT_POINTS
        for name in ("evaluated_mesh_stats", "mesh_signature"):
            builder = getattr(api, name, None)
            if not callable(builder):
                out[name] = {"unavailable": "the add-on does not expose %s in this build" % name}
                continue
            if too_large:
                out[name] = {"skipped": "the evaluated mesh has %d vertices (above the %d vertex guard for "
                                        "the legacy pure-Python helpers); signature_sha256 above reports the "
                                        "same change signal" % (vertex_count, MAX_EXACT_POINTS)}
                continue
            try:
                out[name] = builder(obj, deps)
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": "%s: %s" % (type(exc).__name__, exc)}
        return out

    def modifier_rows(obj, deps):
        rows, truncated = [], 0
        for index, mod in enumerate(obj.modifiers):
            if index >= MAX_MODIFIERS:
                truncated = len(obj.modifiers) - MAX_MODIFIERS
                break
            builder = getattr(api, "_modifier_info", None)
            if callable(builder):
                try:
                    rows.append(builder(obj, mod, deps))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            rows.append({"name": mod.name, "type": mod.type,
                         "show_viewport": bool(mod.show_viewport),
                         "show_render": bool(mod.show_render),
                         "node_group": getattr(getattr(mod, "node_group", None), "name", None)})
        return {"items": rows, "truncated": truncated}

    def evaluate(obj, deps, scene):
        result = {
            "object": {
                "name": obj.name,
                "type": obj.type,
                "data": getattr(obj.data, "name", None),
                "data_type": getattr(getattr(obj.data, "bl_rna", None), "identifier", None),
                "hide_viewport": bool(getattr(obj, "hide_viewport", False)),
                "modifiers": len(obj.modifiers),
            },
            "base": {},
            "evaluated_object": None,
            "counts": {},
            "bounds": None,
            "bounds_sources": [],
            "mesh": None,
            "curve": None,
            "pointcloud": None,
            "instances": None,
            "read_support": {},
            "warnings": [],
            "unknowns": [],
            "mesh_cleanup": {"to_mesh_calls": 0, "to_mesh_clear_calls": 0},
            "evaluated_stack": "full modifier stack (this tool never evaluates a partial stack: Blender "
                               "can only do that by modifying the object)",
        }
        if obj.type == "MESH" and obj.data is not None:
            result["base"] = {"vertices": len(obj.data.vertices), "edges": len(obj.data.edges),
                              "polygons": len(obj.data.polygons), "loops": len(obj.data.loops),
                              "materials": len(obj.data.materials) if obj.data.materials else 0}
        elif obj.type == "CURVE" and obj.data is not None:
            result["base"] = {"splines": len(obj.data.splines),
                              "points": sum(len(spline.points) if getattr(spline, "type", None) in
                                            ("NURBS", "POLY") else len(spline.bezier_points)
                                            for spline in obj.data.splines)}
        try:
            evaluated = obj.evaluated_get(deps)
        except Exception as exc:  # noqa: BLE001
            fail("object_not_evaluated",
                 "object %r is not part of the evaluated scene %r (%s); it is probably excluded from the "
                 "view layer or not linked into the scene" % (obj.name, scene.name, exc),
                 details={"scene": scene.name, "object": obj.name})
        data = getattr(evaluated, "data", None)
        data_type = getattr(getattr(data, "bl_rna", None), "identifier", None)
        result["evaluated_object"] = {
            "type": getattr(evaluated, "type", None),
            "data": getattr(data, "name", None),
            "data_type": data_type,
            "matrix_world": _matrix_rows(evaluated.matrix_world),
            "is_instancer": bool(getattr(evaluated, "is_instancer", False)),
        }
        parts = []
        if data_type == "Mesh":
            try:
                mesh = evaluated.to_mesh()
            except Exception as exc:  # noqa: BLE001
                result["read_support"]["mesh"] = "unavailable"
                result["warnings"].append("evaluated mesh could not be read: %s: %s"
                                          % (type(exc).__name__, exc))
                mesh = None
            if mesh is not None:
                result["mesh_cleanup"]["to_mesh_calls"] += 1
                try:
                    matrix = evaluated.matrix_world
                    points, method = world_points(mesh.vertices, matrix)
                    result["counts"] = {"vertices": len(mesh.vertices), "edges": len(mesh.edges),
                                        "polygons": len(mesh.polygons), "loops": len(mesh.loops)}
                    result["mesh"] = {"name": mesh.name, "vertices": len(mesh.vertices),
                                      "edges": len(mesh.edges), "polygons": len(mesh.polygons),
                                      "loops": len(mesh.loops),
                                      "attributes": len(getattr(mesh, "attributes", []) or []),
                                      "uv_layers": len(getattr(mesh, "uv_layers", []) or []),
                                      "color_attributes": len(getattr(mesh, "color_attributes", []) or [])}
                    mesh_bounds = _bounds(points)
                    if mesh_bounds:
                        mesh_bounds["source"] = "evaluated mesh vertices (world space)"
                        parts.append(mesh_bounds)
                    result["bounds_sources"].append({"source": "evaluated_mesh", **method})
                    if not method.get("exact", True):
                        result["warnings"].append(
                            "the evaluated mesh has %d vertices; the world bounds use a stride sample "
                            "(%d points) and are approximate" % (len(mesh.vertices), len(points)))
                    checksum, sampled = signature_sha256(points)
                    result["signature_sha256"] = {"checksum": checksum, "points": sampled,
                                                 "algorithm": "sha256 of sorted, 5-decimal rounded "
                                                              "world-space vertices"}
                    result["read_support"]["mesh"] = "evaluated_mesh (to_mesh/to_mesh_clear)"
                finally:
                    evaluated.to_mesh_clear()
                    result["mesh_cleanup"]["to_mesh_clear_calls"] += 1
        elif data_type == "Curve":
            try:
                result["curve"] = curve_report(data, evaluated.matrix_world)
                result["counts"] = {"splines": result["curve"]["splines"],
                                    "curve_points": result["curve"]["total_points"]}
                if result["curve"]["bounds"]:
                    result["curve"]["bounds"]["source"] = "curve control points (world space)"
                    parts.append(result["curve"]["bounds"])
                result["bounds_sources"].append({"source": "evaluated_curve_control_points",
                                                 "exact": False,
                                                 "note": "the tessellated surface may extend beyond the "
                                                         "control points"})
                result["read_support"]["curve"] = "evaluated curve datablock (splines/control points)"
            except Exception as exc:  # noqa: BLE001
                result["read_support"]["curve"] = "unavailable"
                result["warnings"].append("evaluated curve could not be read: %s: %s"
                                          % (type(exc).__name__, exc))
            try:
                mesh = evaluated.to_mesh()
            except Exception as exc:  # noqa: BLE001
                result["read_support"]["curve_tessellation"] = "unavailable"
                result["warnings"].append("curve tessellation could not be read: %s: %s"
                                          % (type(exc).__name__, exc))
                mesh = None
            if mesh is not None:
                result["mesh_cleanup"]["to_mesh_calls"] += 1
                try:
                    result["tessellated"] = {
                        "vertices": len(mesh.vertices), "edges": len(mesh.edges),
                        "polygons": len(mesh.polygons),
                        "source": "evaluated.to_mesh() of the curve (rv3 tessellation)",
                    }
                    result["read_support"]["curve_tessellation"] = "evaluated_mesh"
                finally:
                    evaluated.to_mesh_clear()
                    result["mesh_cleanup"]["to_mesh_clear_calls"] += 1
        elif data_type == "PointCloud":
            try:
                points_bl = data.points
                points, method = world_points(points_bl, evaluated.matrix_world)
                result["pointcloud"] = {"points": len(points_bl)}
                result["counts"] = {"points": len(points_bl)}
                bounds = _bounds(points)
                if bounds:
                    bounds["source"] = "evaluated point cloud (world space)"
                    parts.append(bounds)
                result["bounds_sources"].append({"source": "evaluated_point_cloud", **method})
                result["read_support"]["pointcloud"] = "evaluated point cloud datablock"
            except Exception as exc:  # noqa: BLE001
                result["read_support"]["pointcloud"] = "unavailable"
                result["warnings"].append("evaluated point cloud could not be read: %s: %s"
                                          % (type(exc).__name__, exc))
        elif data_type is None:
            result["read_support"]["geometry"] = "no data block (Empty or instancer-only object)"
        else:
            result["read_support"]["geometry"] = "unsupported_by_this_tool"
            result["unknowns"].append(
                "the evaluated geometry is %r; this tool does not report counts for it (no numbers are "
                "guessed)" % data_type)

        try:
            instances = instance_report(deps, obj)
        except Exception as exc:  # noqa: BLE001
            instances = {"errors": ["instance scan failed: %s: %s" % (type(exc).__name__, exc)]}
        result["instances"] = instances
        if instances.get("bounds"):
            parts.append(instances["bounds"])
        if instances.get("bounds") or instances.get("errors") or instances.get("bounds_unavailable_sources"):
            result["bounds_sources"].append({"source": "instances", "exact": bool(instances.get("complete"))})
        result["bounds"] = _merge_bounds(parts)
        if result["bounds"]:
            result["bounds"]["complete"] = all(bool(part.get("exact", True))
                                               for part in result["bounds_sources"])
        result["components"] = []
        if result.get("mesh") is not None:
            result["components"].append("MESH")
        if result.get("curve") is not None:
            result["components"].append("CURVE")
        if result.get("pointcloud") is not None:
            result["components"].append("POINTCLOUD")
        if instances.get("instances"):
            result["components"].append("INSTANCES")
        if any(mod.type == "NODES" for mod in obj.modifiers):
            result["unknowns"].append("Python evaluated Object.data does not expose every GeometrySet component; internal GN curve/point-cloud/volume counts may be unavailable. Empty mesh counts do not prove an empty GeometrySet.")
        if result["counts"] and result["counts"].get("vertices") == 0 and result["counts"].get("polygons") == 0:
            if not (instances.get("instances") or 0):
                result["warnings"].append(
                    "the evaluated geometry has no vertices and the depsgraph reports no instances for "
                    "this object; internal GeometrySet components may still be visible")
        if result["base"].get("vertices") and result["counts"].get("vertices") == 0:
            result["warnings"].append(
                "the base mesh has %d vertices but the evaluated mesh has 0: a modifier removes the "
                "geometry" % result["base"]["vertices"])
        if result.get("curve") and result["base"].get("splines") is not None \
                and result["curve"]["splines"] != result["base"]["splines"]:
            result["warnings"].append(
                "the evaluated curve has %d splines, the base curve has %d: a modifier changes the spline "
                "set" % (result["curve"]["splines"], result["base"]["splines"]))
        return result

    def state_fingerprint(obj):
        base = obj.data
        return {
            "data_pointer": common.pointer(base),
            "data_name": getattr(base, "name", None),
            "vertices": len(base.vertices) if obj.type == "MESH" and base is not None else None,
            "polygons": len(base.polygons) if obj.type == "MESH" and base is not None else None,
            "matrix_world": _matrix_rows(obj.matrix_world),
            "modifiers": [{"name": mod.name, "type": mod.type, "show_viewport": bool(mod.show_viewport)}
                          for mod in obj.modifiers],
            "datablocks": {"objects": len(bpy.data.objects), "meshes": len(bpy.data.meshes),
                           "curves": len(bpy.data.curves), "node_groups": len(bpy.data.node_groups)},
            "dirty": bool(getattr(bpy.data, "is_dirty", False)),
        }

    def state_delta(before, after):
        delta = {}
        for key in before:
            if before[key] != after.get(key):
                delta[key] = {"before": before[key], "after": after.get(key)}
        return delta

    def focus_modifier(obj, name, deps):
        if name is None:
            return None
        matches = [mod for mod in obj.modifiers if mod.name == name]
        if not matches:
            fail("modifier_not_found", "object %r has no modifier named %r" % (obj.name, name),
                 details={"modifiers": [mod.name for mod in obj.modifiers]})
        mod = matches[0]
        builder = getattr(api, "_modifier_info", None)
        if callable(builder):
            try:
                entry = builder(obj, mod, deps)
            except Exception:  # noqa: BLE001
                entry = None
        else:
            entry = None
        if entry is None:
            entry = {"name": mod.name, "type": mod.type,
                     "show_viewport": bool(mod.show_viewport), "show_render": bool(mod.show_render),
                     "node_group": getattr(getattr(mod, "node_group", None), "name", None)}
        entry["is_geometry_nodes"] = bool(mod.type == "NODES"
                                         and getattr(getattr(mod, "node_group", None), "bl_rna", None)
                                         is not None
                                         and mod.node_group.bl_rna.identifier == "GeometryNodeTree")
        entry["evaluated"] = ("part of the full stack above; this tool does not evaluate a partial stack "
                              "because Blender can only do that by modifying the object")
        warnings = getattr(mod, "node_warnings", None)
        entry["node_warnings"] = ([{"message": getattr(w, "message", str(w)),
                                     "type": getattr(w, "type", None)}
                                    for w in warnings][:MAX_WARNING_REFS]
                                   if warnings is not None else None)
        return entry

    # ----------------------------------------------------------------- #
    # model_inspect
    # ----------------------------------------------------------------- #
    @api.command("model_inspect", mutating=False,
                 summary="evaluated-model summary for one object in its exact scene")
    def model_inspect(object, scene=None, modifier=None):
        """Read-only evaluated geometry facts; never modifies the object or scene."""
        obj = exact_object(object)
        target_scene, holders = scene_scope(obj, scene)
        deps, deps_source = depsgraph_for(target_scene)
        before = state_fingerprint(obj)
        evaluation = evaluate(obj, deps, target_scene)
        deps_scene = getattr(deps, "scene", None)
        deps_scene = getattr(deps_scene, "original", deps_scene)
        payload = {
            "object": evaluation["object"],
            "scene": {
                "name": target_scene.name,
                "requested": scene,
                "scenes_with_object": [item.name for item in holders],
                "context_scene": getattr(getattr(bpy.context, "scene", None), "name", None),
                "frame_current": target_scene.frame_current,
                "unit_system": target_scene.unit_settings.system,
                "depsgraph": {
                    **deps_source,
                    "scene": getattr(deps_scene, "name", None),
                    "matches_target_scene": (deps_scene is target_scene) if deps_scene is not None else None,
                    "note": "the depsgraph is taken from the target scene, never from bpy.context",
                },
            },
            "base": evaluation["base"],
            "evaluated_object": evaluation["evaluated_object"],
            "counts": evaluation["counts"],
            "components": evaluation["components"],
            "bounds": evaluation["bounds"],
            "bounds_sources": evaluation["bounds_sources"],
            "mesh": evaluation["mesh"],
            "tessellated": evaluation.get("tessellated"),
            "curve": evaluation["curve"],
            "pointcloud": evaluation["pointcloud"],
            "instances": evaluation["instances"],
            "signature_sha256": evaluation.get("signature_sha256"),
            "modifiers": modifier_rows(obj, deps),
            "focus_modifier": focus_modifier(obj, modifier, deps),
            "read_support": evaluation["read_support"],
            "warnings": evaluation["warnings"],
            "unknowns": evaluation["unknowns"],
            "legacy": legacy_stats(obj, deps, (evaluation["counts"] or {}).get("vertices")),
            "mesh_cleanup": evaluation["mesh_cleanup"],
            "evaluated_stack": evaluation["evaluated_stack"],
            "changed_nothing": True,
            "notes": [
                "counts and bounds describe the evaluated (modifier-applied) result in the target scene",
                "the object's mesh data, modifier stack and transform are read-only for this command",
            ],
        }
        if deps_scene is not None and deps_scene is not target_scene:
            payload["warnings"].append(
                "the depsgraph reports scene %r but %r was requested"
                % (getattr(deps_scene, "name", None), target_scene.name))
        after = state_fingerprint(obj)
        delta = state_delta(before, after)
        payload["state_unchanged"] = {"unchanged": not delta, "delta": delta,
                                     "compared": sorted(before)}
        if "dirty" in delta:
            payload["warnings"].append(
                "bpy.data.is_dirty changed during a read-only command; nothing was saved by this tool")
        payload["warnings"] = payload["warnings"][:MAX_WARNING_REFS]
        return payload

    def compact_summary(obj, scene):
        """The evaluated summary embedded in a viewport capture (post-draw)."""
        deps, deps_source = depsgraph_for(scene)
        evaluation = evaluate(obj, deps, scene)
        return {
            "object": evaluation["object"],
            "scene": scene.name,
            "counts": evaluation["counts"],
            "bounds": evaluation["bounds"],
            "signature_sha256": evaluation.get("signature_sha256"),
            "instances": {"instances": (evaluation["instances"] or {}).get("instances"),
                          "sources": (evaluation["instances"] or {}).get("sources")},
            "modifiers": [{"name": mod.name, "type": mod.type, "show_viewport": bool(mod.show_viewport)}
                          for mod in obj.modifiers],
            "read_support": evaluation["read_support"],
            "warnings": evaluation["warnings"],
            "unknowns": evaluation["unknowns"],
            "depsgraph": deps_source,
            "modifier_results_shown": {
                "viewport_draws_evaluated_object": True,
                "modifier_show_viewport": {mod.name: bool(mod.show_viewport) for mod in obj.modifiers},
                "note": "a 3D viewport always draws the evaluated object; show_viewport decides which "
                        "modifiers contribute to it",
            },
        }

    # ----------------------------------------------------------------- #
    # viewport capture
    # ----------------------------------------------------------------- #
    def viewport_state(space):
        """Capture-time view evidence; unavailable fields are explicit nulls."""
        shading = getattr(space, "shading", None)
        region_3d = getattr(space, "region_3d", None)
        report = {"shading_type": getattr(shading, "type", None),
                  "color_type": getattr(shading, "color_type", None),
                  "show_xray": getattr(shading, "show_xray", None),
                  "show_overlays": getattr(getattr(space, "overlay", None), "show_overlays", None),
                  "view_perspective": getattr(region_3d, "view_perspective", None),
                  "view_distance": getattr(region_3d, "view_distance", None)}
        for name in ("view_location", "view_rotation"):
            raw = getattr(region_3d, name, None)
            report[name] = list(raw) if raw is not None else None
        return report

    @api.command("viewport_capture_prepare",
                 summary="arm a real 3D viewport screenshot of one object (deferred commit)")
    def viewport_capture_prepare(object, scene=None, window_index=None, area_index=None, fit=True):
        """Select/frame the object, arm a draw probe, return a session id."""
        started = time.time()
        common.require_gui(api, "capturing the 3D viewport")
        if fit not in FIT_VALUES:
            fail("invalid_argument", "fit must be a boolean", details={"fit": fit})
        obj = exact_object(object)
        target_scene, holders = scene_scope(obj, scene)
        window, window_index = common.resolve_window(api, window_index)

        def is_view3d(area):
            return getattr(area, "type", None) == "VIEW_3D"

        def is_convertible(area):
            if getattr(area, "type", None) in ("TOPBAR", "STATUSBAR", "HEADER"):
                return False
            region = common.region_of(area)
            return region is not None and int(getattr(region, "width", 0) or 0) > 0

        conversion = {"performed": False, "reason": None}
        if area_index is not None:
            areas = common.area_list(window)
            if isinstance(area_index, bool) or not isinstance(area_index, int):
                fail("invalid_argument", "area_index must be an integer")
            if area_index < 0 or area_index >= len(areas):
                fail("area_index_out_of_range",
                     "area_index %d does not exist in the selected window (%d area(s))"
                     % (area_index, len(areas)), details={"areas": common.area_report(window)})
            area = areas[area_index]
            if not is_view3d(area):
                conversion = {"performed": True,
                              "reason": "the requested area is %r, not a 3D viewport; it is reconfigured "
                                        "temporarily" % getattr(area, "type", None)}
        else:
            area, area_index = common.resolve_area(api, window, None, is_view3d, "3D viewport")
            if area is None:
                area, area_index = common.resolve_area(api, window, None, is_convertible, "editor area")
                if area is None:
                    fail("viewport_unavailable",
                         "the selected window has no area that can host a 3D viewport",
                         details={"areas": common.area_report(window), "windows": common.window_report()})
                conversion = {"performed": True,
                              "reason": "the window has no 3D viewport; area %d (%s) is reconfigured "
                                        "temporarily" % (area_index, getattr(area, "type", None))}

        snapshot = common.snapshot_ui(window, area)
        warnings, notes = [], [
            "the image is the whole 3D viewport area (Blender has no cropped area capture)",
            "no camera render and no OpenGL still-render fallback is used",
        ]
        scene_switch = {"changed": False}
        view_before = view_after = None
        frame_report = {"requested": bool(fit), "performed": False}
        try:
            if getattr(area, "type", None) != "VIEW_3D":
                before_type = getattr(area, "type", None)
                try:
                    area.type = "VIEW_3D"
                except Exception as exc:  # noqa: BLE001
                    fail("area_type_change_failed",
                         "Blender refused to turn area %d (%s) into a 3D viewport: %s"
                         % (area_index, before_type, exc), details={"from": before_type})
                if getattr(area, "type", None) != "VIEW_3D":
                    fail("area_type_change_failed",
                         "area %d did not become a 3D viewport (it reports %r)"
                         % (area_index, getattr(area, "type", None)))
                conversion["area_type_changed"] = {"from": before_type, "to": "VIEW_3D"}
            previous_scene, scene_switch = common.switch_window_scene(api, window, target_scene, {})
            layer = getattr(window, "view_layer", None)
            if layer is None:
                fail("context_unavailable", "the selected window exposes no view layer")
            if obj.name not in layer.objects:
                fail("object_not_in_view_layer",
                     "object %r is not part of the view layer %r of scene %r, so the viewport cannot show "
                     "it" % (obj.name, getattr(layer, "name", None), target_scene.name),
                     details={"scene": target_scene.name, "view_layer": getattr(layer, "name", None),
                              "hidden_by": "collection exclusion or a different view layer"})
            visibility = {"in_view_layer": True,
                          "hide_viewport": bool(getattr(obj, "hide_viewport", False))}
            try:
                visibility["hide_get"] = bool(obj.hide_get(view_layer=layer))
            except TypeError:
                try:
                    visibility["hide_get"] = bool(obj.hide_get())
                except Exception:  # noqa: BLE001
                    visibility["hide_get"] = None
            except Exception as exc:  # noqa: BLE001
                visibility["hide_get"] = None
                visibility["hide_error"] = str(exc)
            try:
                visibility["visible_get"] = bool(obj.visible_get())
            except Exception as exc:  # noqa: BLE001
                visibility["visible_get"] = None
                visibility["visible_error"] = str(exc)
            if visibility["hide_viewport"] or visibility["hide_get"]:
                fail("object_not_visible",
                     "object %r is hidden (%s), so a viewport screenshot would not show it; unhide it in "
                     "the Outliner and retry (this tool never changes visibility)"
                     % (obj.name, "hide_viewport" if visibility["hide_viewport"] else "hide_get"),
                     details=visibility)
            if visibility.get("visible_get") is False:
                fail("object_not_visible",
                     "object %r reports visible_get()=False in this view layer; a screenshot would not "
                     "show it" % obj.name, details=visibility)
            region = common.require_region(api, area)
            space = common.space_of(area, "VIEW_3D")
            if space is None:
                fail("space_unavailable", "the viewport area exposes no VIEW_3D space")
            region_3d = getattr(space, "region_3d", None)
            if region_3d is not None:
                try:
                    view_before = {"view_location": [round(float(value), 4) for value in region_3d.view_location],
                                   "view_distance": round(float(region_3d.view_distance), 4),
                                   "view_perspective": str(region_3d.view_perspective)}
                except Exception:  # noqa: BLE001
                    view_before = None
            if fit:
                if getattr(bpy.context, "mode", "OBJECT") != "OBJECT":
                    fail("object_mode_required",
                         "leave edit/pose mode before capturing the object (current mode: %s)"
                         % getattr(bpy.context, "mode", "?"))
                try:
                    with bpy.context.temp_override(window=window, screen=getattr(window, "screen", None),
                                                   area=area, region=region):
                        for other in list(layer.objects):
                            try:
                                if other.select_get(view_layer=layer):
                                    other.select_set(False, view_layer=layer)
                            except Exception:  # noqa: BLE001
                                continue
                        obj.select_set(True, view_layer=layer)
                        layer.objects.active = obj
                        result = bpy.ops.view3d.view_selected(use_all_regions=False)
                        if "FINISHED" not in (result or []):
                            raise RuntimeError("view_selected did not finish")
                    frame_report = {"requested": True, "performed": True, "selected": obj.name,
                                    "select_get": bool(obj.select_get(view_layer=layer)),
                                    "active": getattr(layer.objects.active, "name", None),
                                    "operator": "bpy.ops.view3d.view_selected"}
                except Exception as exc:  # noqa: BLE001
                    fail("fit_failed", "framing %r in the 3D viewport failed: %s" % (obj.name, exc))
            if region_3d is not None:
                try:
                    view_after = {"view_location": [round(float(value), 4) for value in region_3d.view_location],
                                  "view_distance": round(float(region_3d.view_distance), 4),
                                  "view_perspective": str(region_3d.view_perspective)}
                except Exception:  # noqa: BLE001
                    view_after = None
            else:
                warnings.append("this build exposes no SpaceView3D.region_3d, so the view cannot be "
                                "reported or restored")
            area.tag_redraw()
            redraw = force_redraw(window, getattr(window, "screen", None), area, region)
        except Exception:
            common.restore_ui(api, snapshot, window, area, reason="prepare_failed")
            raise

        expect = {
            "space_type": "VIEW_3D",
            "window_scene": target_scene.name,
            "area_type": "VIEW_3D",
            "region": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
        }
        preferences_view = getattr(getattr(bpy.context, "preferences", None), "view", None)
        if fit and preferences_view is not None:
            smooth_ms = max(0, min(1000, int(getattr(preferences_view, "smooth_view", 0))))
            expect["draw_after_epoch"] = time.time() + smooth_ms / 1000.0 + 0.12
        payload = common.arm_session(
            api, "model_inspection_capture_viewport", window, window_index, getattr(window, "screen", None),
            area, area_index, region, space, snapshot, expect, started, warnings, notes,
            tree=None, tree_lookup=None, extra_restore=None,
            metadata_provider=lambda: {
                "evaluated": compact_summary(obj, target_scene),
                "viewport": viewport_state(space),
                "target": {"object": obj.name, "object_type": obj.type, "scene": target_scene.name,
                           "in_scene": obj.name in target_scene.objects,
                           "scenes_with_object": [item.name for item in holders]},
            },
        )
        payload["conversion"] = conversion
        area.tag_redraw()
        payload["scene_switch"] = scene_switch
        payload["target"] = {"object": obj.name, "scene": target_scene.name,
                             "scenes_with_object": [item.name for item in holders]}
        payload["visibility"] = visibility
        payload["viewport"] = {
            "fit_requested": bool(fit), "fit": frame_report,
            "view_before": view_before, "view_after": view_after,
            "space_type": getattr(space, "type", None),
            "region_size": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
            "restore_note": "selection, active object, window scene and the view are restored by commit/"
                            "abort, also when the capture fails",
        }
        payload["redraw_timer"] = redraw
        return payload

    def force_redraw(window, screen, area, region):
        operator = getattr(getattr(bpy.ops, "wm", None), "redraw_timer", None)
        if operator is None:
            return {"attempted": False, "reason": "bpy.ops.wm.redraw_timer is unavailable in this build"}
        try:
            with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
                result = operator(type="DRAW_WIN_SWAP", iterations=1)
            return {"attempted": True, "operator": "bpy.ops.wm.redraw_timer", "type": "DRAW_WIN_SWAP",
                    "iterations": 1, "result": sorted(set(result or []))}
        except Exception as exc:  # noqa: BLE001
            return {"attempted": True, "operator": "bpy.ops.wm.redraw_timer", "failed": True,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    @api.command("viewport_capture_status", mutating=False,
                 summary="draw-probe state of an armed viewport capture")
    def viewport_capture_status(session):
        return common.status_payload(api, session)

    @api.command("viewport_capture_commit",
                 summary="screenshot the armed viewport area, publish it and restore the UI")
    def viewport_capture_commit(session, relative_path, overwrite=False):
        if not isinstance(overwrite, bool):
            fail("invalid_argument", "overwrite must be boolean")
        return common.commit_capture(api, session, relative_path, overwrite)

    @api.command("viewport_capture_abort",
                 summary="restore the UI of an armed viewport capture session")
    def viewport_capture_abort(session):
        return common.abort_session(api, session)
