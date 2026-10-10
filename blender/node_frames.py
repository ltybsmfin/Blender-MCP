"""Native Frame commands. No optional extension is imported here."""
from __future__ import annotations

import math
import uuid
import bpy

TEXT_OWNER = "_mcp_native_frame_text"
FRAME_OWNER = "_mcp_native_frame_text_owner"
MAX_BODY = 16384


def register_commands(api):
    error = api.CommandError

    def name(value, label):
        if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 255:
            raise error("invalid_name", label + " must be a nonempty exact name (max 255 UTF-8 bytes)")
        return value

    def tree_named(group, write=False):
        name(group, "group")
        matches = [t for t in bpy.data.node_groups if t.name == group]
        if len(matches) != 1:
            raise error("ambiguous_name" if matches else "node_group_not_found", "expected one exact node group: " + group)
        tree = matches[0]
        if tree.bl_idname not in {"GeometryNodeTree", "ShaderNodeTree", "CompositorNodeTree", "TextureNodeTree"}:
            raise error("unsupported_tree", "native Frame tools do not support " + tree.bl_idname)
        if write and (tree.library or tree.override_library):
            raise error("read_only_tree", "linked/override node trees cannot be edited")
        return tree

    def node_named(tree, value, frame=False):
        name(value, "frame" if frame else "node")
        matches = [n for n in tree.nodes if n.name == value]
        if len(matches) != 1:
            raise error("ambiguous_name" if matches else "node_not_found", "expected one exact node in %s: %s" % (tree.name, value))
        node = matches[0]
        if frame and node.bl_idname != "NodeFrame":
            raise error("wrong_node_type", "%s is not a Frame" % value)
        return node

    def point(value, label, size=2, low=-1e6, high=1e6):
        if not isinstance(value, (list, tuple)) or len(value) != size or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high for v in value):
            raise error("invalid_value", "%s must contain %d finite values in [%s,%s]" % (label, size, low, high))
        return tuple(float(v) for v in value)

    def same(a, b):
        return a is not None and b is not None and a.as_pointer() == b.as_pointer()

    def world(node):
        # RNA in 5.2 supports writing location_absolute, but summing local
        # positions also works before a node editor has drawn the graph.
        result = [float(v) for v in node.location]
        parent, seen = node.parent, {node.as_pointer()}
        while parent:
            if parent.as_pointer() in seen:
                raise error("parent_cycle", "existing parent hierarchy contains a cycle")
            seen.add(parent.as_pointer())
            result = [a + float(b) for a, b in zip(result, parent.location)]
            parent = parent.parent
        return result

    def set_world(node, xy):
        base = world(node.parent) if node.parent else [0, 0]
        node.location = [xy[0] - base[0], xy[1] - base[1]]

    def references(text):
        trees = list(bpy.data.node_groups)
        for collection in (bpy.data.materials, bpy.data.worlds, bpy.data.lights, bpy.data.scenes):
            for block in collection:
                embedded = getattr(block, "node_tree", None)
                if embedded:
                    trees.append(embedded)
        unique = {t.as_pointer(): t for t in trees}
        return [{"group": t.name, "frame": n.name} for t in unique.values() for n in t.nodes
                if n.bl_idname == "NodeFrame" and same(n.text, text)]

    def text_report(text):
        if text is None:
            return None
        return {"name": text.name, "body": text.as_string(), "users": text.users,
                "references": references(text), "internal": bool(text.get(TEXT_OWNER)), "filepath": text.filepath}

    def report(tree, frame):
        return {"group": tree.name, "frame": frame.name, "label": frame.label,
                "location": list(frame.location), "location_absolute": world(frame), "width": frame.width,
                "height": frame.height, "dimensions": list(frame.dimensions),
                "parent": frame.parent.name if frame.parent else None,
                "children": [n.name for n in tree.nodes if same(n.parent, frame)],
                "color": list(frame.color), "use_custom_color": frame.use_custom_color,
                "label_size": frame.label_size, "shrink": frame.shrink, "text": text_report(frame.text)}

    def attrs(label=None, color=None, label_size=None, shrink=None, width=None, height=None):
        out = {}
        if label is not None:
            if not isinstance(label, str) or "\x00" in label or len(label) > 256:
                raise error("invalid_value", "label must be a string up to 256 characters")
            out["label"] = label
        if color is not None:
            out.update(color=point(color, "color", 3, 0, 1), use_custom_color=True)
        if label_size is not None:
            if type(label_size) is not int or not 8 <= label_size <= 64:
                raise error("invalid_value", "label_size must be an integer 8..64")
            out["label_size"] = label_size
        if shrink is not None:
            if type(shrink) is not bool:
                raise error("invalid_value", "shrink must be boolean")
            out["shrink"] = shrink
        for key, value in (("width", width), ("height", height)):
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value) or not 100 <= value <= 10000:
                    raise error("invalid_value", key + " must be finite, 100..10000")
                out[key] = float(value)
        return out

    def snapshot(tree):
        return [(n, n.parent, tuple(n.location), float(n.width)) for n in tree.nodes]

    def restore(rows):
        for n, parent, _, _ in rows:
            n.parent = parent
        for n, _, location, width in rows:
            n.location, n.width = location, width

    @api.command("node_frame_create", summary="create/reuse a native Frame in an exact node group")
    def create(group, frame, label="", location=(0, 0), color=None, label_size=24, shrink=False):
        tree = tree_named(group, True)
        name(frame, "frame")
        xy = point(location, "location")
        settings = attrs(label, color, label_size, shrink)
        if tree.nodes.get(frame):
            target = node_named(tree, frame, True)
            return {"created": False, "skipped": [frame], **report(tree, target)}
        target = None
        try:
            target = tree.nodes.new("NodeFrame")
            target.name = frame
            if target.name != frame:
                raise error("invalid_name", "Blender truncated the requested Frame name")
            target.location = xy
            for key, value in settings.items():
                setattr(target, key, value)
            tree.update_tag()
            return {"created": True, "skipped": [], **report(tree, target)}
        except Exception as exc:
            if target:
                tree.nodes.remove(target)
            if isinstance(exc, error):
                raise
            raise error("frame_write_failed", str(exc), details={"rolled_back": True}) from exc

    @api.command("node_frame_get", mutating=False, summary="query a native Frame and its Text")
    def get(group, frame):
        tree = tree_named(group)
        return report(tree, node_named(tree, frame, True))

    def parent_change(group, frame, nodes, attach, create_frame=False):
        tree = tree_named(group, True)
        name(frame, "frame")
        if not isinstance(nodes, list) or not nodes or len(nodes) > 512:
            raise error("invalid_nodes", "nodes must be a nonempty list, max 512 names")
        if type(create_frame) is not bool:
            raise error("invalid_value", "create_frame must be boolean")
        # Resolve every target before creating a Frame or changing any parent.
        targets, skipped = [], []
        for value in nodes:
            node = node_named(tree, value)
            if any(same(node, n) for n in targets):
                skipped.append(value)
            else:
                targets.append(node)
        target = node_named(tree, frame, True) if tree.nodes.get(frame) else None
        if target is None and not (attach and create_frame):
            raise error("node_not_found", "Frame missing; create_frame defaults to false")
        for n in targets:
            cursor = target
            while cursor:
                if same(cursor, n):
                    raise error("parent_cycle", "cannot parent a Frame to itself or its descendant")
                cursor = cursor.parent
            if n.name == frame:
                raise error("parent_cycle", "cannot parent a Frame to itself")
        rows = snapshot(tree)
        coordinates = {n.name: world(n) for n in tree.nodes}
        created = False
        changed = []
        try:
            if target is None:
                create(group, frame)
                target, created = node_named(tree, frame, True), True
            # Outer parents first; setting descendants afterwards compensates
            # for any outer change, including overlapping Frame selections.
            def depth(n):
                count, p = 0, n.parent
                while p:
                    count, p = count + 1, p.parent
                return count
            for n in sorted(targets, key=depth):
                if attach and same(n.parent, target) or not attach and not same(n.parent, target):
                    skipped.append(n.name)
                    continue
                n.parent = target if attach else None
                set_world(n, coordinates[n.name])
                changed.append(n.name)
            for n in tree.nodes:
                if n.name in coordinates and any(abs(a - b) > 0.005 for a, b in zip(world(n), coordinates[n.name])):
                    raise error("layout_changed", "parent edit moved a node: " + n.name)
            tree.update_tag()
        except Exception as exc:
            try:
                restore(rows)
                if created:
                    tree.nodes.remove(target)
            except Exception as rollback:
                raise error("frame_write_failed", str(exc), details={"rolled_back": False, "rollback_error": str(rollback)}) from exc
            if isinstance(exc, error):
                raise
            raise error("frame_write_failed", str(exc), details={"rolled_back": True}) from exc
        return {"changed": changed, "skipped": skipped, "created": created,
                "world_positions_preserved": True, **report(tree, target)}

    @api.command("node_frame_parent", summary="atomically parent exact nodes while keeping canvas positions")
    def parent(group, frame, nodes, create_frame=False):
        return parent_change(group, frame, nodes, True, create_frame)

    @api.command("node_frame_unparent", summary="detach direct children while keeping canvas positions")
    def unparent(group, frame, nodes):
        return parent_change(group, frame, nodes, False)

    @api.command("node_frame_update", summary="atomic native Frame label/color/layout properties")
    def update(group, frame, label=None, color=None, label_size=None, shrink=None, width=None, height=None):
        tree = tree_named(group, True)
        target = node_named(tree, frame, True)
        settings = attrs(label, color, label_size, shrink, width, height)
        previous = {key: tuple(getattr(target, key)) if key == "color" else getattr(target, key) for key in settings}
        try:
            for key, value in settings.items():
                setattr(target, key, value)
            tree.update_tag()
        except Exception as exc:
            try:
                for key, value in previous.items():
                    setattr(target, key, value)
            except Exception as rollback:
                raise error("frame_write_failed", str(exc), details={"rolled_back": False, "rollback_error": str(rollback)}) from exc
            raise error("frame_write_failed", str(exc), details={"rolled_back": True}) from exc
        return {"updated": list(settings), "skipped": [], **report(tree, target)}

    @api.command("node_frame_set_text", summary="stable internal Text; explicit user-text overwrite and empty policy")
    def set_text(group, frame, body, overwrite=False, allow_empty=False):
        tree = tree_named(group, True)
        target = node_named(tree, frame, True)
        if not isinstance(body, str) or len(body) > MAX_BODY or "\x00" in body:
            raise error("invalid_body", "body must be a string up to %d characters without NUL" % MAX_BODY)
        if not body and not allow_empty:
            raise error("empty_body", "empty text requires allow_empty=true; it retains the Text datablock")
        if type(overwrite) is not bool or type(allow_empty) is not bool:
            raise error("invalid_value", "overwrite and allow_empty must be boolean")
        text = target.text
        owned = text is not None and text.get(TEXT_OWNER) == target.get(FRAME_OWNER) and bool(target.get(FRAME_OWNER))
        if text is not None:
            refs = references(text)
            if len(refs) > 1 or text.users - int(text.use_fake_user) > 1:
                raise error("shared_text", "refusing to change shared Text", details={"references": refs, "users": text.users})
            if text.library or text.override_library or text.filepath:
                raise error("external_text", "linked/override/external Text cannot be overwritten")
            if not owned and not overwrite:
                raise error("text_exists", "user Text requires overwrite=true; no data was changed")
        previous_text = text
        previous_body = text.as_string() if text else None
        owner_before = target.get(FRAME_OWNER)
        created = False
        try:
            if text is None:
                owner = uuid.uuid4().hex
                text = bpy.data.texts.new("MCPFrameText_" + owner)
                created = True
                text[TEXT_OWNER] = owner
                target[FRAME_OWNER] = owner
                target.text = text
            if text.as_string() != body:
                text.from_string(body)
            tree.update_tag()
            return {"created_text": created, "updated": previous_body != body, "skipped": [frame] if previous_body == body else [], **report(tree, target)}
        except Exception as exc:
            try:
                target.text = previous_text
                if created:
                    bpy.data.texts.remove(text)
                    if owner_before is None:
                        if FRAME_OWNER in target:
                            del target[FRAME_OWNER]
                    else:
                        target[FRAME_OWNER] = owner_before
                elif text:
                    text.from_string(previous_body)
            except Exception as rollback:
                raise error("frame_write_failed", str(exc), details={"rolled_back": False, "rollback_error": str(rollback)}) from exc
            raise error("frame_write_failed", str(exc), details={"rolled_back": True}) from exc

    @api.command("node_frame_layout", mutating=False, summary="native nodes/Frames/Text layout summary; never rearranges")
    def layout(group):
        tree = tree_named(group)
        return {"group": tree.name, "tree_type": tree.bl_idname,
                "frames": [report(tree, n) for n in tree.nodes if n.bl_idname == "NodeFrame"],
                "nodes": [{"name": n.name, "label": n.label, "parent": n.parent.name if n.parent else None,
                           "location_absolute": world(n), "width": n.width, "dimensions": list(n.dimensions)} for n in tree.nodes],
                "links": [{"from_node": l.from_node.name, "from_socket": l.from_socket.identifier,
                           "to_node": l.to_node.name, "to_socket": l.to_socket.identifier} for l in tree.links]}
