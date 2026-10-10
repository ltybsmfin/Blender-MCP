"""Geometry Nodes editor evidence: machine-readable layout + **real** screenshots.

Bridge-side command module, registered by ``blender_mcp_addon.py`` exactly like
``node_frames.py`` or ``file_open.py``::

    import geometry_nodes_capture
    geometry_nodes_capture.register_commands(sys.modules[__name__])

Commands
--------
``gn_editor_layout``
    Read-only structure report for one exact ``GeometryNodeTree``: nodes (type,
    label, canvas coordinates, parent Frame, absolute coordinates, size), every
    link with both endpoints and the multi-input order, Frame/Reroute details and
    the group interface.  Nothing is written; the view is never touched.
``gn_editor_capture_prepare`` / ``_status`` / ``_commit`` / ``_abort``
    One deferred capture session for the *real* Geometry Nodes editor area.  The
    screenshot is ``bpy.ops.screen.screenshot_area`` output - the actual editor
    pixels - and it is **never** replaced by a render, a camera image, a cached
    file or a cropped/synthesised picture.

Why a session (and why the capture is deferred)
-----------------------------------------------
The add-on's bridge handler runs synchronously inside a ``bpy.app.timers``
callback on Blender's main thread, so it **cannot** pump the UI event loop and
therefore cannot force an editor redraw from inside one command.  A capture is
only meaningful after the requested editor state has actually been drawn, so:

1. ``prepare`` snapshots the target window/area state, points the editor at the
   requested node group, optionally fits the view, marks the area for redraw,
   installs a real ``Space.draw_handler_add`` probe and returns a session id.
2. The MCP tool polls ``status`` until the probe reports at least one frame
   drawn *while the space showed the requested tree* (the probe reads the space
   state inside the draw pass, not just when the command ran).
3. ``commit`` re-verifies the area, takes the screenshot, validates and
   atomically publishes the PNG under ``Project/previews``, records the layout
   and restores every temporary UI change.  Without an observed draw it fails
   with ``stale_frame`` instead of returning an old/blank image.

Honest limits
-------------
* The image is the whole editor area: Blender exposes no area sub-rectangle
  capture, so a ``nodes`` subset changes the *fit* and the metadata only.
* ``node.height``/``node.dimensions`` are RNA values that can need one draw to
  be exact; the report marks whether they are already non-zero.
* No mouse/keyboard synthesis, no window activation, no focus stealing, no
  ``.blend`` save, no node coordinate or link is modified.
"""

from __future__ import annotations

import time

import bpy

try:  # registered as a top-level module by the add-on
    from . import review_capture_common as common
except ImportError:  # pragma: no cover - the add-on path
    import review_capture_common as common

#: Bounded sizes so one call can never serialise an unbounded graph.
NODE_LIMIT = 4000
SOCKET_LIMIT = 48
LINK_LIMIT = 6000
SUBSET_LIMIT = 512
#: Fit modes accepted by the editor tool.
FIT_MODES = ("KEEP", "ALL", "SELECTED")
#: Areas that must never be borrowed for a temporary editor conversion.
PROTECTED_AREA_TYPES = ("TOPBAR", "STATUSBAR", "HEADER")


def register_commands(api):
    error = api.CommandError

    def fail(code, message, details=None):
        raise error(code, message, details=details)

    # ----------------------------------------------------------------- #
    # target resolution
    # ----------------------------------------------------------------- #
    def exact_object(name):
        if not isinstance(name, str) or not name.strip():
            fail("invalid_argument", "object must be a non-empty exact object name")
        obj = bpy.data.objects.get(name)
        if obj is None:
            fail("object_not_found", "no object named %r" % name,
                 details={"available": [item.name for item in bpy.data.objects][:50]})
        return obj

    def exact_modifier(obj, name):
        matches = [mod for mod in obj.modifiers if mod.name == name]
        if not matches:
            fail("modifier_not_found", "object %r has no modifier named %r" % (obj.name, name),
                 details={"modifiers": [{"name": mod.name, "type": mod.type} for mod in obj.modifiers]})
        return matches[0]

    def exact_group(name):
        if not isinstance(name, str) or not name.strip():
            fail("invalid_argument", "group must be a non-empty exact node group name")
        matches = [tree for tree in bpy.data.node_groups if tree.name == name]
        if not matches:
            fail("node_group_not_found", "no node group named %r" % name,
                 details={"available": [tree.name for tree in bpy.data.node_groups][:50]})
        return matches[0]

    def resolve_target(group, object=None, modifier=None):
        """Resolve ``group`` (+ optional object/modifier) to one exact node tree."""
        obj = exact_object(object) if object is not None else None
        mod = None
        if modifier is not None:
            if obj is None:
                fail("invalid_argument", "modifier requires object")
            mod = exact_modifier(obj, modifier)
            if mod.type != "NODES":
                fail("wrong_modifier_type",
                     "modifier %r on %s is %s, not a Geometry Nodes modifier" % (mod.name, obj.name, mod.type),
                     details={"modifier": mod.name, "type": mod.type})
            if getattr(mod, "node_group", None) is None:
                fail("modifier_has_no_node_group",
                     "modifier %r on %s has no node group assigned" % (mod.name, obj.name))
        elif obj is not None:
            candidates = [item for item in obj.modifiers
                          if item.type == "NODES" and getattr(item, "node_group", None) is not None
                          and item.node_group.name == group]
            if not candidates:
                nodes_modifiers = [{"name": item.name,
                                    "node_group": getattr(getattr(item, "node_group", None), "name", None)}
                                   for item in obj.modifiers if item.type == "NODES"]
                fail("modifier_not_found",
                     "object %r has no Geometry Nodes modifier using node group %r" % (obj.name, group),
                     details={"nodes_modifiers": nodes_modifiers})
            if len(candidates) > 1:
                fail("ambiguous_modifier",
                     "object %r has %d Geometry Nodes modifiers using %r; pass modifier=<name>"
                     % (obj.name, len(candidates), group),
                     details={"modifiers": [item.name for item in candidates]})
            mod = candidates[0]
        tree = exact_group(group)
        if tree.bl_idname != "GeometryNodeTree":
            fail("unsupported_tree",
                 "node group %r is a %s; this tool only reads Geometry Node trees" % (group, tree.bl_idname),
                 details={"bl_idname": tree.bl_idname})
        if mod is not None:
            assigned = getattr(mod, "node_group", None)
            if assigned is not None and assigned.name != tree.name:
                fail("group_modifier_mismatch",
                     "modifier %r uses node group %r, not %r" % (mod.name, assigned.name, tree.name),
                     details={"modifier_group": assigned.name, "requested_group": tree.name})
        return {
            "group": tree,
            "object": obj,
            "modifier": mod,
            "resolution": {
                "group": tree.name,
                "object": obj.name if obj is not None else None,
                "modifier": mod.name if mod is not None else None,
                "modifier_type": mod.type if mod is not None else None,
                "group_users": int(getattr(tree, "users", 0) or 0),
                "linked": bool(getattr(tree, "library", None) or getattr(tree, "override_library", None)),
            },
        }

    def resolve_subset(tree, nodes, required=False):
        if nodes is None:
            if required:
                fail("invalid_argument", "nodes is required for this request")
            return None
        if not isinstance(nodes, list) or not nodes:
            fail("invalid_argument", "nodes must be a non-empty list of exact node names")
        if len(nodes) > SUBSET_LIMIT:
            fail("invalid_argument", "nodes accepts at most %d names" % SUBSET_LIMIT,
                 details={"count": len(nodes), "limit": SUBSET_LIMIT})
        resolved, missing, duplicates = [], [], []
        for name in nodes:
            if not isinstance(name, str) or not name.strip():
                fail("invalid_argument", "every entry of nodes must be a non-empty node name")
            node = tree.nodes.get(name)
            if node is None:
                missing.append(name)
                continue
            if any(existing is node for existing in resolved):
                duplicates.append(name)
                continue
            resolved.append(node)
        if missing:
            fail("node_not_found", "no node named %s in %r" % (", ".join(repr(m) for m in missing), tree.name),
                 details={"missing": missing, "available": [node.name for node in tree.nodes][:50]})
        return {"nodes": resolved, "duplicates": duplicates}

    # ----------------------------------------------------------------- #
    # geometry helpers (pure data: no drawing required)
    # ----------------------------------------------------------------- #
    def absolute_location(node):
        result = [float(value) for value in node.location]
        parent = getattr(node, "parent", None)
        seen = {node.as_pointer()}
        while parent is not None:
            if parent.as_pointer() in seen:
                break
            seen.add(parent.as_pointer())
            base = [float(value) for value in parent.location]
            result = [a + b for a, b in zip(result, base)]
            parent = getattr(parent, "parent", None)
        return [round(value, 4) for value in result]

    def frame_ancestors(node):
        chain, parent = [], getattr(node, "parent", None)
        seen = {node.as_pointer()}
        while parent is not None:
            if parent.as_pointer() in seen:
                break
            seen.add(parent.as_pointer())
            chain.append(parent)
            parent = getattr(parent, "parent", None)
        return chain

    def visibility(node):
        """Is this node drawn, and which ancestor explains it if not?"""
        ancestors = frame_ancestors(node)
        if bool(getattr(node, "hide", False)):
            for ancestor in ancestors:
                if not bool(getattr(ancestor, "shrink", False)):
                    return False, "hidden", ancestor.name
            return False, "hidden", ancestors[-1].name if ancestors else None
        for ancestor in ancestors:
            if bool(getattr(ancestor, "shrink", False)):
                return False, "inside_collapsed_frame", ancestor.name
        return True, "drawn", node.name

    def node_size(node):
        width = float(getattr(node, "width", 0.0) or 0.0)
        height = None
        try:
            height = float(getattr(node, "height", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            height = None
        dimensions = None
        try:
            dimensions = [round(float(value), 4) for value in list(node.dimensions)[:2]]
        except Exception:  # noqa: BLE001
            dimensions = None
        return {
            "width": round(width, 4),
            "height": None if height is None else round(height, 4),
            "dimensions": dimensions,
            "size_reported": height is not None and (height > 0 or bool(dimensions and any(dimensions))),
        }

    def socket_row(socket, index):
        row = {
            "index": index,
            "name": getattr(socket, "name", None),
            "identifier": getattr(socket, "identifier", None),
            "type": getattr(socket, "type", None),
            "socket_type": getattr(getattr(socket, "bl_rna", None), "identifier", None),
            "is_linked": bool(getattr(socket, "is_linked", False)),
            "enabled": bool(getattr(socket, "enabled", True)),
            "hide": bool(getattr(socket, "hide", False)),
        }
        for field in ("is_multi_input", "is_unavailable", "link_limit"):
            if hasattr(socket, field):
                try:
                    row[field] = getattr(socket, field)
                except Exception:  # noqa: BLE001
                    continue
        return row

    def node_row(node, index):
        row = {
            "index": index,
            "name": node.name,
            "label": getattr(node, "label", ""),
            "bl_idname": node.bl_idname,
            "type": getattr(node, "type", None),
            "location": [round(float(value), 4) for value in node.location],
            "location_absolute": absolute_location(node),
            "parent": getattr(getattr(node, "parent", None), "name", None),
            "parent_chain": [ancestor.name for ancestor in frame_ancestors(node)],
            "mute": bool(getattr(node, "mute", False)),
            "hide": bool(getattr(node, "hide", False)),
            "select": bool(getattr(node, "select", False)),
            "internal_links": len(getattr(node, "internal_links", []) or []),
        }
        row.update(node_size(node))
        visible, reason, source = visibility(node)
        row["visible_in_editor"] = visible
        row["visibility_reason"] = reason
        row["visible_via"] = source
        inputs = list(getattr(node, "inputs", []) or [])
        outputs = list(getattr(node, "outputs", []) or [])
        row["input_count"] = len(inputs)
        row["output_count"] = len(outputs)
        row["inputs"] = [socket_row(socket, position) for position, socket in enumerate(inputs[:SOCKET_LIMIT])]
        row["outputs"] = [socket_row(socket, position) for position, socket in enumerate(outputs[:SOCKET_LIMIT])]
        if len(inputs) > SOCKET_LIMIT:
            row["inputs_truncated"] = len(inputs) - SOCKET_LIMIT
        if len(outputs) > SOCKET_LIMIT:
            row["outputs_truncated"] = len(outputs) - SOCKET_LIMIT
        if node.bl_idname == "NodeFrame":
            text = getattr(node, "text", None)
            row["frame"] = {
                "label": getattr(node, "label", ""),
                "label_size": getattr(node, "label_size", None),
                "shrink": bool(getattr(node, "shrink", False)),
                "color": [round(float(value), 4) for value in getattr(node, "color", [])],
                "use_custom_color": bool(getattr(node, "use_custom_color", False)),
                "text": None if text is None else {"name": text.name, "body": text.as_string()[:4096]},
                "children": [child.name for child in node.id_data.nodes
                             if getattr(child, "parent", None) is not None
                             and child.parent.as_pointer() == node.as_pointer()],
            }
        if node.bl_idname == "NodeReroute":
            reroute = {"socket_type": None, "inputs": len(inputs), "outputs": len(outputs)}
            if inputs:
                reroute["socket_type"] = getattr(inputs[0], "type", None)
            row["reroute"] = reroute
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            row["node_group"] = nested.name
            row["node_group_type"] = nested.bl_rna.identifier
        return row

    def link_row(link, index):
        row = {
            "index": index,
            "from_node": getattr(getattr(link, "from_node", None), "name", None),
            "from_node_label": getattr(getattr(link, "from_node", None), "label", None),
            "from_socket": getattr(getattr(link, "from_socket", None), "identifier", None),
            "from_socket_index": getattr(getattr(link, "from_socket", None), "index", None),
            "from_socket_name": getattr(getattr(link, "from_socket", None), "name", None),
            "to_node": getattr(getattr(link, "to_node", None), "name", None),
            "to_socket": getattr(getattr(link, "to_socket", None), "identifier", None),
            "to_socket_index": getattr(getattr(link, "to_socket", None), "index", None),
            "to_socket_name": getattr(getattr(link, "to_socket", None), "name", None),
            "to_socket_multi_input": bool(getattr(getattr(link, "to_socket", None), "is_multi_input", False)),
        }
        for field in ("multi_input_sort_id", "is_valid", "is_hidden"):
            if hasattr(link, field):
                try:
                    value = getattr(link, field)
                except Exception:  # noqa: BLE001
                    continue
                row[field] = value if isinstance(value, (bool, int, float, str)) else str(value)
        return row

    def interface_payload(tree):
        builder = getattr(api, "_interface_payload", None)
        if callable(builder):
            try:
                return builder(tree)
            except Exception:  # noqa: BLE001
                pass
        items = []
        try:
            for item in tree.interface.items_tree:
                items.append({
                    "name": getattr(item, "name", None),
                    "identifier": getattr(item, "identifier", None),
                    "in_out": getattr(item, "in_out", None),
                    "socket_type": getattr(item, "socket_type", None),
                    "item_type": getattr(item, "item_type", None),
                    "description": getattr(item, "description", ""),
                })
        except Exception:  # noqa: BLE001
            return {"items": [], "unavailable": "group.interface.items_tree is not readable in this build"}
        return {"items": items}

    def modifier_users(tree):
        builder = getattr(api, "_group_modifier_users", None)
        if callable(builder):
            try:
                return builder(tree)
            except Exception:  # noqa: BLE001
                pass
        users = []
        for obj in bpy.data.objects:
            for mod in obj.modifiers:
                if mod.type == "NODES" and getattr(mod, "node_group", None) is not None \
                        and mod.node_group.name == tree.name:
                    users.append({"object": obj.name, "modifier": mod.name,
                                  "show_viewport": bool(mod.show_viewport),
                                  "show_render": bool(mod.show_render)})
        return users

    def subset_report(tree, subset):
        nodes = subset["nodes"] if subset else list(tree.nodes)
        hidden, collapsed, frames, skipped_collapsed = [], [], [], []
        for node in nodes:
            visible, reason, source = visibility(node)
            if visible:
                continue
            if reason == "hidden":
                hidden.append({"node": node.name, "visible_via": source})
            else:
                collapsed.append({"node": node.name, "collapsed_frame": source})
        for node in nodes:
            for ancestor in frame_ancestors(node):
                if all(existing.as_pointer() != ancestor.as_pointer() for existing in frames):
                    frames.append(ancestor)
        points = []
        for node in nodes + frames:
            location = absolute_location(node)
            size = node_size(node)
            width = size["width"] or 140.0
            height = size["height"] or (size["dimensions"][1] if size["dimensions"] else 100.0) or 100.0
            points.append((location[0], location[1]))
            points.append((location[0] + width, location[1] - height))
            points.append((location[0], location[1] - height))
            points.append((location[0] + width, location[1]))
        report = {
            "requested": None if subset is None else [node.name for node in subset["nodes"]],
            "considered": [node.name for node in nodes],
            "expanded_to_frames": [node.name for node in frames],
            "hidden": hidden,
            "collapsed": collapsed,
            "duplicates": (subset or {}).get("duplicates", []),
            "bounds": None,
        }
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            report["bounds"] = {
                "min": [round(min(xs), 3), round(min(ys), 3)],
                "max": [round(max(xs), 3), round(max(ys), 3)],
                "size": [round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)],
                "source": "node.location plus parent Frames (canvas units); node sizes are RNA values",
            }
        report["skipped_for_fit"] = skipped_collapsed
        return report

    def layout_payload(tree, subset=None, resolution=None, extra=None):
        nodes = list(tree.nodes)
        subset_nodes = subset["nodes"] if subset else []
        subset_pointers = {node.as_pointer() for node in subset_nodes}
        reported = list(nodes)
        truncated = 0
        if len(reported) > NODE_LIMIT:
            keep = [node for node in reported if node.as_pointer() in subset_pointers]
            for node in reported:
                if len(keep) >= NODE_LIMIT:
                    break
                if node.as_pointer() not in {item.as_pointer() for item in keep}:
                    keep.append(node)
            truncated = len(reported) - len(keep)
            reported = keep
        node_rows = [node_row(node, index) for index, node in enumerate(reported)]
        sizes_reliable = all(row.get("size_reported") for row in node_rows) if node_rows else None
        links = list(tree.links)
        link_rows = [link_row(link, index) for index, link in enumerate(links[:LINK_LIMIT])]
        payload = {
            "group": {
                "name": tree.name,
                "bl_idname": tree.bl_idname,
                "tree_type": "GeometryNodeTree",
                "users": int(getattr(tree, "users", 0) or 0),
                "library": getattr(getattr(tree, "library", None), "filepath", None),
                "override_library": bool(getattr(tree, "override_library", None)),
                "is_embedded": bool(getattr(tree, "is_embedded_data", False)),
                "fake_user": bool(getattr(tree, "use_fake_user", False)),
            },
            "resolution": resolution or {"group": tree.name},
            "interface": interface_payload(tree),
            "modifier_users": modifier_users(tree),
            "nodes": node_rows,
            "links": link_rows,
            "counts": {
                "nodes": len(nodes),
                "nodes_reported": len(node_rows),
                "nodes_truncated": truncated,
                "frames": sum(1 for node in nodes if node.bl_idname == "NodeFrame"),
                "reroutes": sum(1 for node in nodes if node.bl_idname == "NodeReroute"),
                "links": len(links),
                "links_reported": len(link_rows),
                "links_truncated": max(len(links) - LINK_LIMIT, 0),
                "groups": sum(1 for node in nodes if getattr(node, "node_tree", None) is not None),
            },
            "subset": subset_report(tree, subset),
            "read_support": {
                "node_location": "RNA Node.location (exact without a draw)",
                "node_location_absolute": "node.location plus every parent Frame (canvas units)",
                "node_size": "RNA node.width/node.height/node.dimensions; height/dimensions can need one "
                             "editor draw to be exact",
                "node_size_reliable_now": sizes_reliable,
                "multi_input_order": "NodeLink.multi_input_sort_id" if link_rows and "multi_input_sort_id"
                                     in link_rows[0] else "unavailable_in_this_build",
                "socket_defaults": "not evaluated by this command (use the node/socket tools)",
                "draw_required_properties": "socket visibility, node height and wire routing are only final "
                                            "after the editor has drawn the tree",
            },
            "limits": {"nodes": NODE_LIMIT, "sockets_per_node": SOCKET_LIMIT, "links": LINK_LIMIT},
            "image_scope": {
                "note": "a screenshot of this editor is the whole editor area; Blender exposes no cropped "
                        "area capture, so a 'nodes' subset is expressed as a view fit plus these bounds",
                "capture_area_required": "NODE_EDITOR with tree_type=GeometryNodeTree showing this group",
            },
        }
        if extra:
            payload.update(extra)
        return payload

    # ----------------------------------------------------------------- #
    # editor selection / fit
    # ----------------------------------------------------------------- #
    def snapshot_node_selection(tree):
        rows = []
        for node in tree.nodes:
            try:
                rows.append((node, bool(node.select)))
            except Exception:  # noqa: BLE001
                continue
        return rows

    def restore_node_selection(rows):
        changes, failures = [], []
        for node, selected in rows or []:
            try:
                if bool(node.select) != selected:
                    node.select = selected
                    changes.append(node.name)
            except Exception as exc:  # noqa: BLE001
                failures.append("%s: %s" % (getattr(node, "name", "?"), exc))
        return {"restored": not failures, "changes": changes, "failures": failures}

    def select_for_fit(tree, subset, warnings):
        """Select what must be framed; report what cannot be framed."""
        selected, skipped = [], []
        candidates = subset["nodes"] if subset else [node for node in tree.nodes if bool(getattr(node, "select", False))]
        if not candidates:
            fail("empty_selection",
                 "fit='SELECTED' needs a node selection: pass nodes=[...] or select nodes in the editor")
        for node in candidates:
            visible, reason, source = visibility(node)
            if visible:
                selected.append(node)
                continue
            if reason == "inside_collapsed_frame" and source:
                frame = tree.nodes.get(source)
                if frame is not None:
                    selected.append(frame)
                    skipped.append({"node": node.name, "framed_by": source})
                    warnings.append("node %r is inside the collapsed Frame %r, so the Frame is framed"
                                    % (node.name, source))
                else:
                    skipped.append({"node": node.name, "framed_by": None})
                    warnings.append("node %r is inside a collapsed Frame and could not be framed" % node.name)
            else:
                skipped.append({"node": node.name, "framed_by": None})
                warnings.append("node %r is hidden (node.hide) and cannot be framed by Blender" % node.name)
        for node in tree.nodes:
            wanted = any(node.as_pointer() == item.as_pointer() for item in selected)
            try:
                if bool(node.select) != wanted:
                    node.select = wanted
            except Exception:  # noqa: BLE001
                continue
        return {
            "selected": [node.name for node in selected],
            "skipped": skipped,
            "used_existing_selection": subset is None,
        }

    def apply_fit(window, screen, area, region, space, tree, subset, fit, warnings):
        report = {"requested": fit, "changed": False, "operator": None, "selection": None}
        if fit == "KEEP":
            report["note"] = "fit='KEEP' leaves the editor view untouched"
            return report
        override = {"window": window, "screen": screen, "area": area, "region": region}
        if fit == "SELECTED":
            report["selection"] = select_for_fit(tree, subset, warnings)
            try:
                with bpy.context.temp_override(**override):
                    result = bpy.ops.node.view_selected()
                report["operator"] = "bpy.ops.node.view_selected"
                report["operator_result"] = sorted(set(result or []))
                report["changed"] = "FINISHED" in (result or [])
            except Exception as exc:  # noqa: BLE001
                fail("fit_failed", "bpy.ops.node.view_selected failed: %s" % exc,
                     details={"selection": report["selection"]})
        else:
            try:
                with bpy.context.temp_override(**override):
                    result = bpy.ops.node.view_all()
                report["operator"] = "bpy.ops.node.view_all"
                report["operator_result"] = sorted(set(result or []))
                report["changed"] = "FINISHED" in (result or [])
            except Exception as exc:  # noqa: BLE001
                fail("fit_failed", "bpy.ops.node.view_all failed: %s" % exc)
        if not report["changed"]:
            fail("fit_failed", "the requested node view operator did not finish", details=report)
        region_3d = getattr(space, "region_3d", None)
        if region_3d is not None:
            try:
                report["view_after"] = {
                    "view_location": [round(float(value), 4) for value in region_3d.view_location],
                    "view_distance": round(float(region_3d.view_distance), 4),
                }
            except Exception:  # noqa: BLE001
                report["view_after"] = None
        else:
            report["view_after"] = None
            report["view_readable"] = False
        return report

    def force_redraw(window, screen, area, region):
        """Best-effort synchronous redraw of the target window (probe-confirmed)."""
        operator = getattr(getattr(bpy.ops, "wm", None), "redraw_timer", None)
        if operator is None:
            return {"attempted": False, "reason": "bpy.ops.wm.redraw_timer is unavailable in this build"}
        try:
            with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
                result = operator(type="DRAW_WIN_SWAP", iterations=1)
            return {"attempted": True, "operator": "bpy.ops.wm.redraw_timer",
                    "type": "DRAW_WIN_SWAP", "iterations": 1, "result": sorted(set(result or []))}
        except Exception as exc:  # noqa: BLE001
            return {"attempted": True, "operator": "bpy.ops.wm.redraw_timer", "failed": True,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    # ----------------------------------------------------------------- #
    # commands
    # ----------------------------------------------------------------- #
    @api.command("gn_editor_layout", mutating=False,
                 summary="machine-readable Geometry Nodes layout (nodes/Frames/Reroutes/links)")
    def gn_editor_layout(group, object=None, modifier=None, nodes=None):
        """Return the real canvas structure of one Geometry Node group; never edits."""
        resolved = resolve_target(group, object, modifier)
        tree = resolved["group"]
        subset = resolve_subset(tree, nodes)
        payload = layout_payload(tree, subset, resolved["resolution"])
        payload["changed_nothing"] = True
        return payload

    @api.command("gn_editor_capture_prepare",
                 summary="arm a real Geometry Nodes editor screenshot (deferred commit)")
    def gn_editor_capture_prepare(group, object=None, modifier=None, window_index=None, area_index=None,
                                  fit="KEEP", nodes=None, include_sidebar=True):
        """Point a real editor at the group, fit optionally, arm a draw probe."""
        started = time.time()
        common.require_gui(api, "capturing the Geometry Nodes editor")
        mode = str(fit or "KEEP").upper()
        if mode not in FIT_MODES:
            fail("invalid_argument", "fit must be one of %s" % (list(FIT_MODES),), details={"fit": fit})
        if not isinstance(include_sidebar, bool):
            fail("invalid_argument", "include_sidebar must be boolean")
        resolved = resolve_target(group, object, modifier)
        tree = resolved["group"]
        subset = resolve_subset(tree, nodes)
        warnings = []
        window, window_index = common.resolve_window(api, window_index)

        def is_geometry_editor(area):
            if getattr(area, "type", None) != "NODE_EDITOR":
                return False
            space = common.space_of(area, "NODE_EDITOR")
            if space is None:
                return False
            shown = getattr(space, "node_tree", None)
            tree_type = getattr(space, "tree_type", None)
            return tree_type == "GeometryNodeTree" or (
                shown is not None and getattr(shown, "bl_idname", None) == "GeometryNodeTree")

        def is_other_node_editor(area):
            return getattr(area, "type", None) == "NODE_EDITOR"

        def is_convertible(area):
            if getattr(area, "type", None) in PROTECTED_AREA_TYPES:
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
            if not is_geometry_editor(area):
                conversion = {"performed": True, "reason": "the requested area is %r, not a Geometry Nodes "
                                                          "editor; it is reconfigured temporarily"
                                                          % getattr(area, "type", None)}
        else:
            area, area_index = common.resolve_area(api, window, None, is_geometry_editor,
                                                   "Geometry Nodes editor")
            if area is None:
                area, area_index = common.resolve_area(api, window, None, is_other_node_editor,
                                                       "node editor")
                if area is not None:
                    conversion = {"performed": True,
                                  "reason": "the window has a node editor showing another tree type; it is "
                                            "pointed at the requested group temporarily"}
            if area is None:
                area, area_index = common.resolve_area(api, window, None, is_convertible, "editor area")
                if area is None:
                    fail("editor_unavailable",
                         "no area of the selected window can host a Geometry Nodes editor",
                         details={"areas": common.area_report(window), "windows": common.window_report()})
                conversion = {"performed": True,
                              "reason": "the window has no node editor; area %d (%s) is reconfigured "
                                        "temporarily" % (area_index, getattr(area, "type", None))}

        snapshot = common.snapshot_ui(window, area)
        node_selection = snapshot_node_selection(tree)
        fit_report = None
        display = None
        try:
            if getattr(area, "type", None) != "NODE_EDITOR":
                before_type = getattr(area, "type", None)
                try:
                    area.type = "NODE_EDITOR"
                except Exception as exc:  # noqa: BLE001
                    fail("area_type_change_failed",
                         "Blender refused to turn area %d (%s) into a node editor: %s"
                         % (area_index, before_type, exc),
                         details={"area_index": area_index, "from": before_type,
                                  "areas": common.area_report(window)})
                if getattr(area, "type", None) != "NODE_EDITOR":
                    fail("area_type_change_failed",
                         "area %d did not become a node editor (it reports %r)"
                         % (area_index, getattr(area, "type", None)))
                conversion["area_type_changed"] = {"from": before_type, "to": "NODE_EDITOR"}
            region = common.require_region(api, area)
            space = common.space_of(area, "NODE_EDITOR")
            if space is None:
                fail("space_unavailable", "the node editor area exposes no NODE_EDITOR space")
            if not any(s.get("pointer") == common.pointer(space)
                       for s in snapshot["area"].get("spaces", [])):
                snapshot["area"].setdefault("spaces", []).append(common.snapshot_space(space))
            with bpy.context.temp_override(window=window, area=area, region=region):
                display = common.show_node_tree(api, space, tree, "GeometryNodeTree")
            if hasattr(space, "show_region_ui"):
                try:
                    space.show_region_ui = bool(include_sidebar)
                except Exception as exc:  # noqa: BLE001
                    warnings.append("the sidebar (show_region_ui) could not be set: %s" % exc)
            # A new editor/sidebar needs a draw to establish node dimensions and
            # the usable canvas before view_all/view_selected calculates its fit.
            area.tag_redraw()
            initial_redraw = force_redraw(window, getattr(window, "screen", None), area, region)
            region = common.region_of(area) or region
            fit_report = apply_fit(window, screen_for(area, window), area, region, space, tree, subset,
                                   mode, warnings)
            region = common.region_of(area) or region
            area.tag_redraw()
            redraw = force_redraw(window, getattr(window, "screen", None), area, region)
        except Exception:
            common.restore_ui(api, snapshot, window, area, reason="prepare_failed",
                              tree_lookup=tree_lookup,
                              extra_restore=lambda report: restore_node_selection(node_selection))
            raise

        expect = {
            "space_type": "NODE_EDITOR",
            "tree_name": tree.name,
            "tree_pointer": common.pointer(tree),
            "tree_type": "GeometryNodeTree",
            "area_type": "NODE_EDITOR",
            "region": [int(getattr(region, "width", 0) or 0), int(getattr(region, "height", 0) or 0)],
        }
        preferences_view = getattr(getattr(bpy.context, "preferences", None), "view", None)
        if mode != "KEEP" and preferences_view is not None:
            # view_all/selected can finish before their smooth navigation ends.
            # Wait between bridge calls, never block Blender's event loop or
            # change the user's animation preference.
            smooth_ms = max(0, min(1000, int(getattr(preferences_view, "smooth_view", 0))))
            expect["draw_after_epoch"] = time.time() + smooth_ms / 1000.0 + 0.12
        payload = common.arm_session(
            api, "geometry_nodes_capture_editor", window, window_index, getattr(window, "screen", None),
            area, area_index, region, space, snapshot, expect, started, warnings,
            [
                "the image is the whole editor area (Blender has no cropped area capture)",
                "only the view/selection/editor state is touched; node coordinates and links are unchanged",
            ],
            tree=tree, tree_lookup=tree_lookup,
            extra_restore=lambda report: restore_node_selection(node_selection),
            metadata_provider=lambda: {
                "node_layout": layout_payload(tree, subset, resolved["resolution"]),
                "capture_subset": subset_report(tree, subset),
                "fitted_nodes": ((fit_report or {}).get("selection") or {}).get("selected"),
            },
        )
        payload["conversion"] = conversion
        # The probe is installed by arm_session, so redraw only now counts.
        redraw = force_redraw(window, getattr(window, "screen", None), area, region)
        payload["display"] = display
        payload["fit"] = fit_report
        payload["redraw_timer"] = redraw
        payload["initial_redraw_timer"] = initial_redraw
        payload["sidebar_requested"] = bool(include_sidebar)
        return payload

    def screen_for(area, window):
        return getattr(window, "screen", None)

    def tree_lookup(snapshot):
        """Restore the exact original tree, including a borrowed Shader Editor."""
        name = (snapshot or {}).get("node_tree")
        if not name:
            return None
        expected_pointer = snapshot.get("node_tree_pointer")
        candidates = list(bpy.data.node_groups)
        for collection in ("materials", "worlds", "lights", "scenes"):
            for owner in getattr(bpy.data, collection, []) or []:
                for attribute in ("node_tree", "compositing_node_group"):
                    candidate = getattr(owner, attribute, None)
                    if candidate is not None:
                        candidates.append(candidate)
        for candidate in candidates:
            if candidate.name == name and (not expected_pointer
                                          or common.pointer(candidate) == expected_pointer):
                return candidate
        raise RuntimeError("original node tree %r no longer exists; refusing an ambiguous replacement" % name)

    @api.command("gn_editor_capture_status", mutating=False,
                 summary="draw-probe state of an armed Geometry Nodes capture")
    def gn_editor_capture_status(session):
        """Report whether the requested tree was drawn; never captures."""
        return common.status_payload(api, session)

    @api.command("gn_editor_capture_commit",
                 summary="screenshot the armed editor area, publish it and restore the UI")
    def gn_editor_capture_commit(session, relative_path, overwrite=False):
        """Refuse stale frames; publish atomically under Project/previews."""
        if not isinstance(overwrite, bool):
            fail("invalid_argument", "overwrite must be boolean")
        return common.commit_capture(api, session, relative_path, overwrite)

    @api.command("gn_editor_capture_abort",
                 summary="restore the UI of an armed Geometry Nodes capture session")
    def gn_editor_capture_abort(session):
        return common.abort_session(api, session)
