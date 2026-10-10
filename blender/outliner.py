"""Native Outliner / Collection commands: collection-DAG queries and membership edits.

This module is imported by ``blender_mcp_addon._register_outliner_commands()`` and
publishes its bridge commands through ``register_commands(api)``.  It imports only
``bpy`` (plus the Python standard library), never the ``mcp`` package, so it works
in a stock Blender installation, and it never touches ``bpy.context`` and never
runs an operator, so GUI and ``--background`` behave identically.

Native API basis (Blender 5.2.1 LTS)
------------------------------------
* ``Scene.collection`` is the scene's *master* collection - the only root this
  module uses.  Nothing here reads the active scene, the active view layer, the
  window manager or any other UI context.
* ``Collection.children`` (``CollectionChildren.link`` / ``.unlink``) relates a
  collection to a parent collection.  A collection may have **several parents** and
  two scenes may link collections the other one links too, so the collection graph
  is a **DAG**, not a folder tree: one collection datablock can appear at several
  places of the outliner and in several scenes.
* ``Collection.objects`` (``CollectionObjects.link`` / ``.unlink``) is the object
  membership relation; the reverse view is ``Object.users_collection`` (an object
  may be linked into many collections).  ``Collection.all_objects`` and
  ``Collection.children_recursive`` are read-only convenience views.
* ``Collection.hide_viewport`` / ``Collection.hide_render`` are the two native
  visibility flags written by ``outliner_set_visibility``.
* ``Collection.library``, ``Collection.override_library`` and
  ``ID.is_library_indirect`` identify linked/overridden data; those are never
  written.
* Geometry-node groups (``bpy.data.node_groups``) are **global** datablocks: they
  are not owned by, and cannot be linked into, a collection.  No command here
  reads or writes them, and ``outliner_hierarchy`` reports them as global data
  instead of pretending they are collection members.
* Locally created collection names are unique per file, so a name that already
  exists somewhere else cannot be created at a second location without Blender
  silently renaming it to ``name.001``.  This module refuses that
  (``collection_name_conflict``) instead of inventing a suffixed name.

Safety contract
---------------
* The empty string addresses the scene root **only** where an existing root is
  looked up; ``outliner_ensure_path`` requires at least one segment.
* Every write validates the whole batch (scene, path, objects, target and source
  collections) before the first mutation, skips work that is already done
  (idempotent), reports the ownership before *and* after, and never deletes an
  Object or a Collection.  A write into a collection that is shared across scenes
  or that has several direct parents is refused, because the change would be
  visible through another scene or another hierarchy route.
* A failure after a mutation has started is compensated explicitly and reported
  honestly (``outliner_write_failed`` with ``applied`` / ``compensated`` /
  ``residual``); a partially applied batch is never described as a success or as a
  clean rollback.
* ``outliner_ensure_path`` removes only the collections it created itself in that
  same call, and only while they have no other users (no objects, no children, no
  other parent).
"""

from __future__ import annotations

import bpy

# --------------------------------------------------------------------------- #
# bounds (every traversal and every payload is finite)
# --------------------------------------------------------------------------- #
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENTS = 64
MAX_NAME_BYTES = 255
MAX_OBJECTS = 512
MAX_UNLINK_FROM = 64
MAX_SCENE_COLLECTIONS = 2000
MAX_FILE_COLLECTIONS = 5000
MAX_FILE_EDGES = 20000
MAX_DEPTH = 32
MAX_PATHS_PER_COLLECTION = 8
MAX_DIRECT_OBJECTS = 200
MAX_OBJECT_ROWS = 2000
MAX_MEMBERSHIPS = 32
MAX_NAME_SAMPLE = 20

LIMITS = {
    "path_bytes": MAX_PATH_BYTES,
    "path_segments": MAX_PATH_SEGMENTS,
    "name_bytes": MAX_NAME_BYTES,
    "objects_per_call": MAX_OBJECTS,
    "unlink_from": MAX_UNLINK_FROM,
    "scene_collections": MAX_SCENE_COLLECTIONS,
    "file_collections": MAX_FILE_COLLECTIONS,
    "file_edges": MAX_FILE_EDGES,
    "depth": MAX_DEPTH,
    "paths_per_collection": MAX_PATHS_PER_COLLECTION,
    "direct_objects": MAX_DIRECT_OBJECTS,
    "object_rows": MAX_OBJECT_ROWS,
    "memberships_per_object": MAX_MEMBERSHIPS,
    "name_sample": MAX_NAME_SAMPLE,
}


def register_commands(api):
    error = api.CommandError

    # ----------------------------------------------------------------- #
    # primitives
    # ----------------------------------------------------------------- #
    def fail(code, message, details=None):
        raise error(code, message, details=details)

    def key(block):
        """Session-stable identity of one datablock.

        ``ID.as_pointer()`` is the real handle: Blender hands out a fresh Python
        wrapper for the same RNA struct, so ``is``/``id()`` comparisons are not
        reliable for ``bpy`` objects.  ``id()`` is only the last-resort fallback
        for test doubles that do not model ``as_pointer``.
        """
        pointer = getattr(block, "as_pointer", None)
        if callable(pointer):
            try:
                return int(pointer())
            except (TypeError, ValueError):
                pass
        return id(block)

    def name_of(block):
        return getattr(block, "name", None)

    def linked(block):
        if getattr(block, "library", None) is not None:
            return True
        if bool(getattr(block, "is_library_indirect", False)):
            return True
        return getattr(block, "override_library", None) is not None

    def overridden(block):
        return getattr(block, "override_library", None) is not None

    def sample(values, limit=MAX_NAME_SAMPLE):
        return [name_of(value) for value in list(values)[:limit]]

    def children_rel(collection):
        try:
            return collection.children
        except Exception as exc:  # noqa: BLE001 - a broken build must be explicit
            fail(
                "api_unsupported",
                "collection %r exposes no readable children relation: %s" % (name_of(collection), exc),
            )

    def children_of(collection):
        return list(children_rel(collection))

    def objects_rel(collection):
        try:
            return collection.objects
        except Exception as exc:  # noqa: BLE001
            fail(
                "api_unsupported",
                "collection %r exposes no readable objects relation: %s" % (name_of(collection), exc),
            )

    def objects_of(collection):
        return list(objects_rel(collection))

    def master_of(scene):
        master = getattr(scene, "collection", None)
        if master is None:
            fail(
                "scene_master_unavailable",
                "scene %r exposes no master collection, so its collection hierarchy cannot be addressed"
                % name_of(scene),
            )
        return master

    def master_or_none(scene):
        try:
            return getattr(scene, "collection", None)
        except Exception:  # noqa: BLE001
            return None

    # ----------------------------------------------------------------- #
    # argument validation (strict names / bools / lists)
    # ----------------------------------------------------------------- #
    def scene_named(value, write=False):
        if not isinstance(value, str) or not value:
            fail("invalid_scene", "scene must be a non-empty exact scene name")
        if "\x00" in value or value != value.strip():
            fail("invalid_scene", "scene must be an exact name without NUL or surrounding whitespace")
        if len(value.encode("utf-8")) > MAX_NAME_BYTES:
            fail("invalid_scene", "scene is longer than %d UTF-8 bytes" % MAX_NAME_BYTES)
        matches = [scene for scene in bpy.data.scenes if name_of(scene) == value]
        if not matches:
            fail(
                "scene_not_found",
                "no scene named %r" % value,
                details={"available": sample(bpy.data.scenes), "scene_count": len(bpy.data.scenes)},
            )
        if len(matches) > 1:
            fail(
                "ambiguous_scene",
                "%d scenes are named %r; exact scene identity is required" % (len(matches), value),
                details={"scene": value, "matches": len(matches)},
            )
        scene = matches[0]
        if write and linked(scene):
            fail(
                "read_only_scene",
                "scene %r is linked from a library or carries a library override, so it is never written" % value,
                details={"scene": value, "library": bool(getattr(scene, "library", None)),
                         "override": overridden(scene)},
            )
        return scene

    def parse_path(value, allow_root, label="path"):
        if not isinstance(value, str):
            fail("invalid_path", "%s must be a string" % label)
        if "\x00" in value:
            fail("invalid_path", "%s must not contain NUL" % label)
        if value == "":
            if allow_root:
                return []
            fail(
                "invalid_path",
                "%s must name at least one collection: the empty string only addresses an existing scene root"
                % label,
            )
        if len(value.encode("utf-8")) > MAX_PATH_BYTES:
            fail("invalid_path", "%s is longer than %d UTF-8 bytes" % (label, MAX_PATH_BYTES))
        if "\\" in value:
            fail("invalid_path", "%s must use '/' as its separator; a backslash is ambiguous here" % label)
        if value.startswith("/") or value.endswith("/"):
            fail("invalid_path", "%s must not start or end with '/'" % label)
        segments = value.split("/")
        if len(segments) > MAX_PATH_SEGMENTS:
            fail("invalid_path", "%s has more than %d segments" % (label, MAX_PATH_SEGMENTS))
        for segment in segments:
            if segment == "":
                fail("invalid_path", "%s contains an empty segment ('//')" % label)
            if segment in (".", ".."):
                fail(
                    "invalid_path",
                    "%s contains a '.' or '..' segment, which is not a collection name here" % label,
                )
            if segment != segment.strip():
                fail("invalid_path", "%s contains a segment with leading/trailing whitespace" % label)
            if len(segment.encode("utf-8")) > MAX_NAME_BYTES:
                fail(
                    "invalid_path",
                    "a segment of %s is longer than %d UTF-8 bytes" % (label, MAX_NAME_BYTES),
                )
        return segments

    def boolean(value, label):
        if type(value) is not bool:
            fail("invalid_argument", "%s must be a boolean" % label)
        return value

    def object_names(values):
        if not isinstance(values, list) or not values:
            fail("invalid_objects", "objects must be a non-empty list of exact object names")
        if len(values) > MAX_OBJECTS:
            fail(
                "invalid_objects",
                "objects accepts at most %d names per call, got %d" % (MAX_OBJECTS, len(values)),
            )
        seen, names = set(), []
        for value in values:
            if not isinstance(value, str) or not value:
                fail("invalid_objects", "every entry of objects must be a non-empty exact object name")
            if "\x00" in value or value != value.strip():
                fail(
                    "invalid_objects",
                    "object name %r must not contain NUL or surrounding whitespace" % value,
                )
            if len(value.encode("utf-8")) > MAX_NAME_BYTES:
                fail("invalid_objects", "object name %r is longer than %d UTF-8 bytes" % (value, MAX_NAME_BYTES))
            if value in seen:
                fail(
                    "duplicate_object",
                    "object %r is listed more than once; a batch must name every object exactly once" % value,
                )
            seen.add(value)
            names.append(value)
        return names

    def object_named(value):
        """Resolve one exact object name, refusing global duplicates (incl. linked ones)."""
        matches = [obj for obj in bpy.data.objects if name_of(obj) == value]
        if not matches:
            fail(
                "object_not_found",
                "no object named %r" % value,
                details={"available": sample(bpy.data.objects), "object_count": len(bpy.data.objects)},
            )
        if len(matches) > 1:
            fail(
                "ambiguous_object",
                "%d datablocks are named %r (a local one and a linked or duplicate one); the exact object "
                "is ambiguous, so nothing was changed" % (len(matches), value),
                details={"object": value, "matches": len(matches)},
            )
        return matches[0]

    # ----------------------------------------------------------------- #
    # the file-wide collection graph (single bounded traversal)
    # ----------------------------------------------------------------- #
    def all_collections():
        """Every collection datablock considered by this module.

        Scene master collections are included explicitly: they are addressed
        through ``Scene.collection`` and, depending on the build, may not be listed
        in ``bpy.data.collections``.
        """
        found, seen = [], set()
        candidates = []
        for scene in bpy.data.scenes:
            master = master_or_none(scene)
            if master is not None:
                candidates.append(master)
        try:
            candidates.extend(list(bpy.data.collections))
        except Exception:  # noqa: BLE001
            pass
        for block in candidates:
            block_key = key(block)
            if block_key in seen:
                continue
            seen.add(block_key)
            found.append(block)
        return found

    def scene_paths(scene, children_map):
        """Every root-relative path inside ``scene``'s collection DAG.

        A collection with several parents is expanded once per route (that is the
        DAG, not an error); a collection that appears again on its *own* ancestor
        chain is a real cycle and is reported instead of being expanded forever.
        """
        paths, cycles = {}, []
        truncated = False
        master = master_or_none(scene)
        if master is None:
            return paths, cycles, truncated
        pending = [(master, "", frozenset())]
        expansions = 0
        while pending:
            node, path, ancestors = pending.pop()
            node_key = key(node)
            if node_key in ancestors:
                cycles.append({"collection": name_of(node), "path": path})
                continue
            recorded = paths.setdefault(node_key, [])
            if path not in recorded:
                if len(recorded) >= MAX_PATHS_PER_COLLECTION:
                    truncated = True
                else:
                    recorded.append(path)
            if len(ancestors) >= MAX_DEPTH:
                truncated = True
                continue
            expansions += 1
            if expansions > MAX_SCENE_COLLECTIONS:
                truncated = True
                break
            kids = children_map.get(node_key)
            if kids is None:
                kids = children_of(node)
            child_ancestors = ancestors | {node_key}
            for index in range(len(kids) - 1, -1, -1):
                child = kids[index]
                child_path = name_of(child) if not path else "%s/%s" % (path, name_of(child))
                pending.append((child, child_path, child_ancestors))
        return paths, cycles, truncated

    def build_index(scene=None):
        """Build the bounded collection graph used by every command.

        Keys: ``node_by_key``, ``parent_map`` (all direct parents, file-wide),
        ``children_map``, ``in_scene`` (collection key -> scene names) and, for the
        requested scene, ``paths`` / ``cycles``.

        ``graph_truncated`` means the *safety-relevant* part (parents, children,
        scene membership) is incomplete, so a write is refused.  ``paths_truncated``
        only means the path *reporting* hit the depth/route bound (a collection
        deeper than the bound simply has no reported path) and never blocks a write.
        """
        parent_map, children_map, node_by_key = {}, {}, {}
        pending, graph_truncated = [], False
        for candidate in all_collections():
            pending.append(candidate)
        if len(pending) > MAX_FILE_COLLECTIONS:
            graph_truncated = True
        edges = 0
        while pending:
            node = pending.pop()
            node_key = key(node)
            if node_key in node_by_key:
                continue
            if len(node_by_key) >= MAX_FILE_COLLECTIONS:
                graph_truncated = True
                continue
            node_by_key[node_key] = node
            kids = children_of(node)
            children_map[node_key] = kids
            for child in kids:
                edges += 1
                if edges > MAX_FILE_EDGES:
                    graph_truncated = True
                    break
                child_key = key(child)
                parent_map.setdefault(child_key, []).append(node)
                if child_key not in node_by_key:
                    pending.append(child)
            if edges > MAX_FILE_EDGES:
                break
        in_scene, scene_truncated = {}, []
        for scene_obj in bpy.data.scenes:
            master = master_or_none(scene_obj)
            if master is None:
                continue
            keys, visited, stack = set(), set(), [master]
            exceeded = False
            while stack:
                node = stack.pop()
                node_key = key(node)
                if node_key in visited:
                    continue
                visited.add(node_key)
                if len(keys) >= MAX_SCENE_COLLECTIONS:
                    exceeded = True
                    break
                keys.add(node_key)
                stack.extend(children_map.get(node_key, []))
            if exceeded:
                scene_truncated.append(name_of(scene_obj))
                graph_truncated = True
            for node_key in keys:
                in_scene.setdefault(node_key, []).append(name_of(scene_obj))
        paths, cycles, paths_truncated = {}, [], False
        if scene is not None:
            paths, cycles, paths_truncated = scene_paths(scene, children_map)
        return {
            "node_by_key": node_by_key,
            "parent_map": parent_map,
            "children_map": children_map,
            "in_scene": in_scene,
            "paths": paths,
            "cycles": cycles,
            "truncated": graph_truncated or paths_truncated,
            "graph_truncated": graph_truncated,
            "paths_truncated": paths_truncated,
            "scene_index_truncated": scene_truncated,
            "collection_count": len(node_by_key),
            "edge_count": edges,
        }

    def index_for(scene, require_complete):
        data = build_index(scene)
        if require_complete and data["graph_truncated"]:
            fail(
                "index_truncated",
                "the collection graph exceeds the traversal bounds (%d collections, %d links), so a single "
                "owner cannot be proven and the write is refused" % (data["collection_count"], data["edge_count"]),
                details={"limits": LIMITS, "collections": data["collection_count"], "edges": data["edge_count"],
                         "scene_index_truncated": data["scene_index_truncated"]},
            )
        return data

    # ----------------------------------------------------------------- #
    # resolution and write guards
    # ----------------------------------------------------------------- #
    def resolve(scene, value, allow_root, label="path"):
        segments = parse_path(value, allow_root, label)
        current = master_of(scene)
        walked = []
        for segment in segments:
            matches = [child for child in children_of(current) if name_of(child) == segment]
            if len(matches) > 1:
                fail(
                    "ambiguous_collection",
                    "%d child collections of %r are named %r; the path is ambiguous and nothing was changed"
                    % (len(matches), name_of(current), segment),
                    details={"path": value, "segment": segment, "parent": name_of(current)},
                )
            if not matches:
                fail(
                    "collection_not_found",
                    "no collection named %r directly under %r" % (segment, name_of(current)),
                    details={"path": value, "segment": segment, "parent": name_of(current),
                             "resolved": "/".join(walked), "available": sample(children_of(current))},
                )
            current = matches[0]
            walked.append(segment)
        return current, "/".join(walked)

    def guard_check(scene, collection, index, label):
        """Whether a membership/visibility write on ``collection`` is safe."""
        if linked(collection):
            return {
                "ok": False,
                "code": "read_only_collection",
                "message": "%s is linked from a library or carries a library override; it is never written" % label,
                "details": {"collection": name_of(collection),
                            "library": bool(getattr(collection, "library", None)),
                            "override": overridden(collection)},
            }
        collection_key = key(collection)
        scene_names = sorted(index["in_scene"].get(collection_key, []))
        if name_of(scene) not in scene_names:
            return {
                "ok": False,
                "code": "collection_outside_scene",
                "message": "%s is not part of scene %r (it belongs to %s)"
                           % (label, name_of(scene), ", ".join(scene_names) or "no scene"),
                "details": {"collection": name_of(collection), "scene": name_of(scene),
                            "collection_scenes": scene_names},
            }
        if len(scene_names) > 1:
            return {
                "ok": False,
                "code": "collection_shared_across_scenes",
                "message": "%s is part of scenes %s, so changing it here would change those scenes too"
                           % (label, ", ".join(scene_names)),
                "details": {"collection": name_of(collection), "scene": name_of(scene),
                            "collection_scenes": scene_names},
            }
        parents = index["parent_map"].get(collection_key, [])
        if len(parents) > 1:
            return {
                "ok": False,
                "code": "collection_multi_parent",
                "message": "%s has %d direct parent collections (%s), so a change here would be visible through "
                           "every one of them" % (label, len(parents), ", ".join(sample(parents))),
                "details": {"collection": name_of(collection), "parents": sample(parents),
                            "parent_count": len(parents)},
            }
        # A singly parented child can still appear along several routes when
        # one of its ancestors is shared. Changing that child changes all routes.
        pending, visited = list(parents), set()
        while pending:
            ancestor = pending.pop()
            ancestor_key = key(ancestor)
            if ancestor_key in visited:
                continue
            visited.add(ancestor_key)
            ancestor_parents = index["parent_map"].get(ancestor_key, [])
            if len(ancestor_parents) > 1:
                return {
                    "ok": False, "code": "collection_multi_parent",
                    "message": "%s descends from shared collection %r; changing it would affect every hierarchy route"
                               % (label, name_of(ancestor)),
                    "details": {"collection": name_of(collection), "shared_ancestor": name_of(ancestor),
                                "parents": sample(ancestor_parents), "parent_count": len(ancestor_parents)},
                }
            pending.extend(ancestor_parents)
        return {"ok": True, "code": "ok", "message": "", "details": {}}

    def guard(scene, collection, index, label):
        result = guard_check(scene, collection, index, label)
        if not result["ok"]:
            fail(result["code"], result["message"], details=result["details"])
        return result

    # ----------------------------------------------------------------- #
    # object membership views
    # ----------------------------------------------------------------- #
    def owners_of(obj, index=None):
        """Every collection that links ``obj`` (``Object.users_collection``)."""
        listed = getattr(obj, "users_collection", None)
        if listed is not None:
            try:
                return list(listed)
            except TypeError:
                pass
        target = key(obj)
        found = []
        candidates = list(index["node_by_key"].values()) if index else all_collections()
        for collection in candidates:
            try:
                if any(key(candidate) == target for candidate in objects_of(collection)):
                    found.append(collection)
            except error:
                continue
        return found

    def member_of(collection, obj, index=None):
        collection_key = key(collection)
        return any(key(owner) == collection_key for owner in owners_of(obj, index))

    def membership(obj, index):
        """Ownership of one object: which collections link it, and where those are.

        ``scene_paths`` is the location inside the requested scene; collections that
        are not part of that scene are reported with their scene names and
        ``outside_scene: true`` instead of guessing a path in a scene that was not
        asked for.
        """
        owners = owners_of(obj, index)
        entries, scenes = [], set()
        for owner in owners[:MAX_MEMBERSHIPS]:
            owner_key = key(owner)
            owner_scenes = sorted(index["in_scene"].get(owner_key, []))
            resolved = list(index["paths"].get(owner_key, []))
            scenes.update(owner_scenes)
            entries.append({
                "collection": name_of(owner),
                "scenes": owner_scenes,
                "scene_paths": resolved,
                "outside_scene": not resolved,
                "linked": linked(owner),
            })
        return {
            "collections": entries,
            "collection_count": len(owners),
            "memberships_truncated": len(owners) > MAX_MEMBERSHIPS,
            "scenes": sorted(scenes),
            "orphan": not owners,
            "linked_object": linked(obj),
            "override_object": overridden(obj),
        }

    def row(node, path, index):
        node_key = key(node)
        parents = index["parent_map"].get(node_key, [])
        scene_names = sorted(index["in_scene"].get(node_key, []))
        kids = index["children_map"].get(node_key, [])
        direct = objects_of(node)
        return {
            "id": node_key,
            "name": name_of(node),
            "path": path,
            "root": path == "",
            "depth": 0 if not path else path.count("/") + 1,
            "parents": [name_of(parent) for parent in parents][:MAX_NAME_SAMPLE],
            "parent_count": len(parents),
            "multi_parent": len(parents) > 1,
            "scenes": scene_names,
            "cross_scene": len(scene_names) > 1,
            "scene_paths": list(index["paths"].get(node_key, [])),
            "linked": linked(node),
            "override": overridden(node),
            "objects": [name_of(obj) for obj in direct][:MAX_DIRECT_OBJECTS],
            "object_count": len(direct),
            "objects_truncated": len(direct) > MAX_DIRECT_OBJECTS,
            "children": [name_of(child) for child in kids][:MAX_NAME_SAMPLE],
            "child_count": len(kids),
        }

    def scene_object_list(scene, index):
        """Objects of one scene, preferring the native ``all_objects`` view."""
        master = master_or_none(scene)
        if master is None:
            return []
        listed = getattr(master, "all_objects", None)
        if listed is not None:
            try:
                return list(listed)
            except TypeError:
                pass
        found, seen = [], set()
        for node_key in index["paths"]:
            node = index["node_by_key"].get(node_key)
            if node is None:
                continue
            for obj in objects_of(node):
                obj_key = key(obj)
                if obj_key not in seen:
                    seen.add(obj_key)
                    found.append(obj)
        return found

    # ----------------------------------------------------------------- #
    # batched mutation with explicit compensation
    # ----------------------------------------------------------------- #
    def run_batch(operation, steps):
        """Apply ``steps`` in order; compensate in reverse on the first failure.

        A failure raises ``outliner_write_failed`` naming what was applied, what
        was compensated and what could not be undone - a partially applied batch is
        never reported as a success or as a clean rollback.
        """
        applied = []
        for step in steps:
            try:
                step["apply"]()
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                compensated, residual = [], []
                for done in reversed(applied):
                    try:
                        done["undo"]()
                        compensated.append(done["label"])
                    except Exception as undo_exc:  # noqa: BLE001
                        residual.append({
                            "step": done["label"],
                            "error": "%s: %s" % (type(undo_exc).__name__, undo_exc),
                        })
                fail(
                    "outliner_write_failed",
                    "Blender refused step %r of %s: %s. %s"
                    % (step["label"], operation, exc,
                       "The steps already applied were undone again."
                       if not residual else
                       "Some steps could not be undone and remain applied (see residual)."),
                    details={
                        "operation": operation,
                        "failed": step["label"],
                        "error": "%s: %s" % (type(exc).__name__, exc),
                        "applied": [done["label"] for done in applied],
                        "compensated": compensated,
                        "residual": residual,
                        "rolled_back": not residual,
                    },
                )
            applied.append(step)
        return [done["label"] for done in applied]

    def cleanup_created(created):
        """Remove only the collections this call created and that are still unused.

        Every name/address is read **before** the datablock is removed: after
        ``bpy.data.collections.remove()`` the RNA struct is freed and touching
        ``node.name`` (or ``as_pointer()``) raises ``ReferenceError``.  Nothing in
        this function reads a removed datablock again, including the error paths -
        the test double invalidates the removed struct exactly like Blender does.
        """
        removed, kept, failed = [], [], []
        for record in reversed(created):
            node = record["node"]
            parent = record["parent"]
            node_name = name_of(node)  # cached while the ID is still valid
            others = []
            for candidate in all_collections():
                if key(candidate) == key(parent):
                    continue
                try:
                    if any(key(child) == key(node) for child in children_of(candidate)):
                        others.append(candidate)
                except error:
                    continue
            used_by = []
            if others:
                used_by.append("additional parent collection(s): %s" % ", ".join(sample(others)))
            try:
                if children_of(node):
                    used_by.append("child collections")
                if objects_of(node):
                    used_by.append("linked objects")
            except error:
                used_by.append("unreadable relations")
            if linked(node):
                used_by.append("linked/override data")
            if used_by:
                kept.append({"collection": node_name, "reason": "; ".join(used_by)})
                continue
            if record["linked"]:
                try:
                    children_rel(parent).unlink(node)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"collection": node_name, "error": "%s: %s" % (type(exc).__name__, exc)})
                    continue
            try:
                bpy.data.collections.remove(node)
                removed.append(node_name)
            except Exception as exc:  # noqa: BLE001
                failed.append({"collection": node_name, "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"removed": removed, "kept": kept, "failed": failed, "clean": not kept and not failed}

    # ----------------------------------------------------------------- #
    # read: outliner_hierarchy
    # ----------------------------------------------------------------- #
    @api.command("outliner_hierarchy", mutating=False,
                 summary="bounded collection DAG, direct objects and global ownership of one exact scene")
    def hierarchy(scene, include_memberships=True):
        boolean(include_memberships, "include_memberships")
        target = scene_named(scene)
        index = index_for(target, require_complete=False)
        rows = []
        for node_key, node_paths in index["paths"].items():
            node = index["node_by_key"].get(node_key)
            if node is None:
                continue
            for path in node_paths:
                entry = row(node, path, index)
                entry["paths_truncated"] = len(node_paths) >= MAX_PATHS_PER_COLLECTION
                rows.append(entry)
        rows.sort(key=lambda item: (item["depth"], item["path"]))
        master = master_of(target)
        view_layers = sorted(name_of(layer) for layer in getattr(target, "view_layers", []))
        payload = {
            "scene": name_of(target),
            "view_layers": view_layers[:MAX_NAME_SAMPLE],
            "view_layer_count": len(view_layers),
            "view_layers_truncated": len(view_layers) > MAX_NAME_SAMPLE,
            "scene_master": {
                "name": name_of(master),
                "path": "",
                "id": key(master),
                "linked": linked(master),
            },
            "include_memberships": include_memberships,
            "collections": rows,
            "collection_count": len(rows),
            "distinct_collection_count": len(index["paths"]),
            "cycles": index["cycles"],
            "truncated": index["truncated"],
            "graph_truncated": index["graph_truncated"],
            "scene_index_truncated": index["scene_index_truncated"],
            "paths_truncated": index["paths_truncated"],
            "index": {
                "file_collections": index["collection_count"],
                "file_edges": index["edge_count"],
                "scene_count": len(list(bpy.data.scenes)),
            },
            # Geometry node groups are global datablocks: reported here so a caller
            # cannot mistake them for collection members.
            "global_data": {
                "node_groups": {
                    "count": len(getattr(bpy.data, "node_groups", []) or []),
                    "collection_member": False,
                    "note": ("Geometry node groups are global bpy.data datablocks and are not owned by any "
                             "collection."),
                },
            },
            "limits": LIMITS,
            "writes_delete_data": False,
        }
        if include_memberships:
            objects = []
            for obj in scene_object_list(target, index):
                if len(objects) >= MAX_OBJECT_ROWS:
                    payload["truncated"] = True
                    payload["object_rows_truncated"] = True
                    break
                objects.append({
                    "name": name_of(obj),
                    "type": getattr(obj, "type", None),
                    "membership": membership(obj, index),
                })
            objects.sort(key=lambda item: item["name"])
            payload["objects"] = objects
            payload["object_count"] = len(objects)
            # Objects that belong to no collection at all are invisible in every
            # outliner hierarchy; they are listed separately (they are exactly the
            # ones ``outliner_unlink_objects`` protects with ``confirm_orphan``).
            in_scene_names = {item["name"] for item in objects}
            collectionless = []
            for obj in list(bpy.data.objects)[:MAX_OBJECT_ROWS]:
                if name_of(obj) in in_scene_names:
                    continue
                if owners_of(obj, index):
                    continue
                collectionless.append(name_of(obj))
            if len(bpy.data.objects) > MAX_OBJECT_ROWS:
                payload["truncated"] = True
            payload["collectionless_objects"] = sorted(collectionless)
        return payload

    # ----------------------------------------------------------------- #
    # write: outliner_ensure_path
    # ----------------------------------------------------------------- #
    @api.command("outliner_ensure_path",
                 summary="create the missing collection segments of one path; reusable and idempotent")
    def ensure_path(scene, path, create_missing=True):
        boolean(create_missing, "create_missing")
        target = scene_named(scene, write=True)
        segments = parse_path(path, allow_root=False)
        index = index_for(target, require_complete=True)

        # ---- phase 1: read-only plan of the COMPLETE path (no bpy write) ----
        # A segment that does not exist yet is a *virtual* placeholder: it is never
        # passed to children_of()/guard()/key(), is never confused with the real
        # scene root, and the rest of the path hangs off it only in the plan.
        master = master_of(target)
        plan, current, missing_names = [], master, []
        name_index = {}

        def file_name_index():
            """Name -> every collection with that exact name (built once per call).

            Locally created collection names are unique per file, so this is the
            authoritative "the name is already taken somewhere else" view.  It is
            built from the live collections only - a planned (virtual) segment is
            never part of it.
            """
            if not name_index:
                seen = set()
                for candidate in list(index["node_by_key"].values()) + all_collections():
                    candidate_key = key(candidate)
                    if candidate_key in seen:
                        continue
                    seen.add(candidate_key)
                    name_index.setdefault(name_of(candidate), []).append(candidate)
            return name_index

        for segment in segments:
            matches = [] if current is None else [
                child for child in children_of(current) if name_of(child) == segment
            ]
            parent_name = None if current is None else name_of(current)
            if len(matches) > 1:
                fail(
                    "ambiguous_collection",
                    "%d child collections of %r are named %r" % (len(matches), parent_name, segment),
                    details={"path": path, "segment": segment, "parent": parent_name},
                )
            if matches:
                plan.append({"segment": segment, "collection": matches[0]})
                current = matches[0]
                continue
            if not create_missing:
                fail(
                    "collection_not_found",
                    "no collection named %r directly under %r" % (segment, parent_name),
                    details={"path": path, "segment": segment, "parent": parent_name,
                             "available": [] if current is None else sample(children_of(current)),
                             "created": [], "create_missing": False},
                )
            if segment in missing_names:
                fail(
                    "collection_name_conflict",
                    "the path asks for two collections named %r; collection names are unique per file, so the "
                    "second one can never be created (no .001 name is invented)" % segment,
                    details={"path": path, "segment": segment, "requested_name": segment,
                             "reason": "duplicated_inside_path", "auto_suffix": False},
                )
            existing = file_name_index().get(segment, [])
            if existing:
                fail(
                    "collection_name_conflict",
                    "a collection named %r already exists elsewhere in this file; creating a second one here "
                    "would make Blender rename it to %r.001, which this server never does - create the path "
                    "under its existing parent or pick another name" % (segment, segment),
                    details={
                        "path": path,
                        "segment": segment,
                        "requested_name": segment,
                        "reason": "exists_elsewhere",
                        "existing": [
                            {"collection": name_of(candidate),
                             "scenes": sorted(index["in_scene"].get(key(candidate), [])),
                             "parents": sample(index["parent_map"].get(key(candidate), [])),
                             "linked": linked(candidate)}
                            for candidate in existing[:MAX_NAME_SAMPLE]
                        ],
                        "auto_suffix": False,
                    },
                )
            missing_names.append(segment)
            plan.append({"segment": segment, "collection": None})
            current = None

        # ---- phase 2: guard every EXISTING collection of the path ----
        # The root and every reused segment (the final one included) must belong to
        # exactly this scene, have a single direct parent and be writable, or the
        # call fails here - before the first new/remove call.
        guard(target, master, index, "scene root collection %r" % name_of(master))
        for entry in plan:
            if entry["collection"] is None:
                continue
            guard(target, entry["collection"], index,
                  "collection %r in the path" % name_of(entry["collection"]))

        # ---- phase 3: create the missing segments ----
        created, reused, created_nodes = [], [], []
        current = master
        try:
            for entry in plan:
                if entry["collection"] is not None:
                    current = entry["collection"]
                    reused.append(entry["segment"])
                    continue
                segment = entry["segment"]
                try:
                    node = bpy.data.collections.new(segment)
                except Exception as exc:  # noqa: BLE001
                    fail(
                        "outliner_write_failed",
                        "Blender refused to allocate a collection named %r: %s" % (segment, exc),
                        details={"path": path, "segment": segment, "created": created, "auto_suffix": False},
                    )
                record = {"node": node, "parent": current, "linked": False}
                created_nodes.append(record)
                if name_of(node) != segment:
                    fail(
                        "invalid_name",
                        "Blender stored the new collection as %r instead of %r; the created datablock is removed "
                        "again and no .001 name is accepted" % (name_of(node), segment),
                        details={"path": path, "segment": segment, "stored_name": name_of(node)},
                    )
                try:
                    children_rel(current).link(node)
                except Exception as exc:  # noqa: BLE001
                    fail(
                        "outliner_write_failed",
                        "the new collection %r could not be linked under %r: %s" % (segment, name_of(current), exc),
                        details={"path": path, "segment": segment, "parent": name_of(current)},
                    )
                record["linked"] = True
                created.append(segment)
                current = node
        except Exception as exc:
            cleanup = cleanup_created(created_nodes)
            if isinstance(exc, error):
                details = dict(exc.details or {})
                details["cleanup"] = cleanup
                details["created"] = created
                details["auto_suffix"] = False
                raise error(exc.code, exc.message, details=details) from exc
            fail(
                "outliner_write_failed",
                "ensuring %r failed: %s" % (path, exc),
                details={"path": path, "created": created, "cleanup": cleanup,
                         "rolled_back": cleanup["clean"]},
            )
        final_index = build_index(target)
        check = guard_check(target, current, final_index, "collection %r" % name_of(current))
        payload = {
            "scene": name_of(target),
            "path": path,
            "created": created,
            "reused": reused,
            "changed": bool(created),
            "idempotent": not created,
            "prechecked": True,
            "auto_suffix": False,
            "collection": row(current, "/".join(segments), final_index),
            "writable": check["ok"],
            "write_guard": {"ok": check["ok"], "code": check["code"], "message": check["message"]},
            "objects_deleted": [],
            "collections_deleted": [],
            "limits": LIMITS,
        }
        if final_index["truncated"]:
            payload["warnings"] = [
                "the collection graph index is bounded and reported truncated after the write, so the returned "
                "ownership view may be incomplete"
            ]
        return payload

    # ----------------------------------------------------------------- #
    # shared write validation
    # ----------------------------------------------------------------- #
    def write_target(scene_name, path, objects):
        """Validate everything a membership write needs before any mutation."""
        target_scene = scene_named(scene_name, write=True)
        index = index_for(target_scene, require_complete=True)
        collection, resolved_path = resolve(target_scene, path, allow_root=True)
        guard(target_scene, collection, index, "target collection %r" % name_of(collection))
        names = object_names(objects)
        return target_scene, index, collection, resolved_path, [(name, object_named(name)) for name in names]

    def write_result(target_scene, collection, resolved_path, index, **extra):
        payload = {
            "scene": name_of(target_scene),
            "path": resolved_path,
            "collection": row(collection, resolved_path, index),
            "objects_deleted": [],
            "collections_deleted": [],
            "warnings": [],
            "limits": LIMITS,
        }
        payload.update(extra)
        return payload

    # ----------------------------------------------------------------- #
    # write: outliner_link_objects
    # ----------------------------------------------------------------- #
    @api.command("outliner_link_objects",
                 summary="link exact objects into one collection; never unlinks anything else")
    def link_objects(scene, path, objects):
        target_scene, index, collection, resolved_path, resolved = write_target(scene, path, objects)
        pending = [(name, obj) for name, obj in resolved if not member_of(collection, obj, index)]
        skipped = [name for name, obj in resolved if member_of(collection, obj, index)]
        before = {name: membership(obj, index) for name, obj in resolved}
        run_batch("outliner_link_objects", [{
            "label": "link %s" % name,
            "apply": (lambda obj=obj: objects_rel(collection).link(obj)),
            "undo": (lambda obj=obj: objects_rel(collection).unlink(obj)),
        } for name, obj in pending])
        after_index = build_index(target_scene)
        after = {name: membership(obj, after_index) for name, obj in resolved}
        return write_result(
            target_scene, collection, resolved_path, after_index,
            linked=[name for name, _ in pending],
            skipped=skipped,
            unlinked=[],
            changed=bool(pending),
            idempotent=not pending,
            before=before,
            after=after,
        )

    # ----------------------------------------------------------------- #
    # write: outliner_move_objects
    # ----------------------------------------------------------------- #
    @api.command("outliner_move_objects",
                 summary="link objects into one collection and unlink exactly the listed same-scene sources")
    def move_objects(scene, path, objects, unlink_from):
        if not isinstance(unlink_from, list):
            fail("invalid_argument", "unlink_from must be a list of collection paths (the empty list is allowed)")
        if len(unlink_from) > MAX_UNLINK_FROM:
            fail(
                "invalid_argument",
                "unlink_from accepts at most %d paths, got %d" % (MAX_UNLINK_FROM, len(unlink_from)),
            )
        target_scene, index, collection, resolved_path, resolved = write_target(scene, path, objects)
        sources, seen_paths = [], set()
        for value in unlink_from:
            segments = parse_path(value, allow_root=True, label="unlink_from entry")
            canonical = "/".join(segments)
            if canonical in seen_paths:
                fail(
                    "duplicate_path",
                    "unlink_from lists %r more than once; each source collection must appear exactly once"
                    % canonical,
                    details={"path": canonical},
                )
            seen_paths.add(canonical)
            source, source_path = resolve(target_scene, value, allow_root=True, label="unlink_from entry")
            if key(source) == key(collection):
                fail(
                    "conflicting_paths",
                    "the target path %r also appears in unlink_from; moving into a collection and unlinking from "
                    "it in the same call is contradictory" % canonical,
                    details={"path": resolved_path, "unlink_from": canonical},
                )
            guard(target_scene, source, index, "source collection %r" % name_of(source))
            sources.append((source_path, source))
        source_keys = {key(source) for _, source in sources}
        pending_links = [(name, obj) for name, obj in resolved if not member_of(collection, obj, index)]
        skipped_links = [name for name, obj in resolved if member_of(collection, obj, index)]
        pending_unlinks = [(name, obj, source_path, source)
                           for name, obj in resolved
                           for source_path, source in sources
                           if member_of(source, obj, index)]
        before = {name: membership(obj, index) for name, obj in resolved}
        # Precompute the whole batch's result before touching anything: the target
        # link is part of the plan, so a move must never detach an object.
        planned = {}
        for name, obj in resolved:
            remaining = [owner for owner in owners_of(obj, index) if key(owner) not in source_keys]
            if not any(key(owner) == key(collection) for owner in remaining):
                remaining.append(collection)
            scenes = sorted({scene_name for owner in remaining
                             for scene_name in index["in_scene"].get(key(owner), [])})
            planned[name] = {
                "collections": [name_of(owner) for owner in remaining],
                "collection_count": len(remaining),
                "scenes": scenes,
                "orphan": not remaining,
            }
        detached = sorted(name for name, state in planned.items() if state["orphan"] or not state["scenes"])
        if detached:
            fail(
                "orphan_object",
                "the batch would leave %s in no scene at all; nothing was changed" % ", ".join(detached),
                details={"objects": detached, "planned": {name: planned[name] for name in detached},
                         "object_deleted": False},
            )
        steps = [{
            "label": "link %s" % name,
            "apply": (lambda obj=obj: objects_rel(collection).link(obj)),
            "undo": (lambda obj=obj: objects_rel(collection).unlink(obj)),
        } for name, obj in pending_links]
        steps.extend([{
            "label": "unlink %s from %s" % (name, source_path or "<root>"),
            "apply": (lambda obj=obj, source=source: objects_rel(source).unlink(obj)),
            "undo": (lambda obj=obj, source=source: objects_rel(source).link(obj)),
        } for name, obj, source_path, source in pending_unlinks])
        run_batch("outliner_move_objects", steps)
        after_index = build_index(target_scene)
        after = {name: membership(obj, after_index) for name, obj in resolved}
        return write_result(
            target_scene, collection, resolved_path, after_index,
            linked=[name for name, _ in pending_links],
            skipped=skipped_links,
            unlinked=["%s@%s" % (name, source_path or "<root>")
                      for name, _, source_path, _ in pending_unlinks],
            unlink_from=sorted(seen_paths),
            planned=planned,
            changed=bool(pending_links or pending_unlinks),
            idempotent=not pending_links and not pending_unlinks,
            before=before,
            after=after,
        )

    # ----------------------------------------------------------------- #
    # write: outliner_unlink_objects
    # ----------------------------------------------------------------- #
    @api.command("outliner_unlink_objects",
                 summary="remove only the membership of exact objects in one collection; never deletes data")
    def unlink_objects(scene, path, objects, confirm_orphan=False):
        boolean(confirm_orphan, "confirm_orphan")
        target_scene, index, collection, resolved_path, resolved = write_target(scene, path, objects)
        collection_key = key(collection)
        pending = [(name, obj) for name, obj in resolved if member_of(collection, obj, index)]
        skipped = [name for name, obj in resolved if not member_of(collection, obj, index)]
        before = {name: membership(obj, index) for name, obj in resolved}
        planned, warnings = {}, []
        for name, obj in pending:
            remaining = [owner for owner in owners_of(obj, index) if key(owner) != collection_key]
            scenes = sorted({scene_name for owner in remaining
                             for scene_name in index["in_scene"].get(key(owner), [])})
            planned[name] = {
                "collection_count": len(remaining),
                "collections": [name_of(owner) for owner in remaining],
                "scenes": scenes,
                "orphan": not remaining,
            }
        blocking = sorted(name for name, state in planned.items() if state["orphan"] or not state["scenes"])
        if blocking and not confirm_orphan:
            fail(
                "orphan_object",
                "unlinking would leave %s in no collection or in no scene at all; nothing was changed. Pass "
                "confirm_orphan=true to remove the link anyway - the object datablock itself is never deleted"
                % ", ".join(blocking),
                details={"objects": blocking, "planned": {name: planned[name] for name in blocking},
                         "confirm_orphan": False, "object_deleted": False},
            )
        if blocking:
            warnings.append(
                "confirm_orphan=true: %s no longer belong(s) to any scene or collection after this call; the "
                "object datablock(s) are preserved and can be linked again" % ", ".join(blocking)
            )
        run_batch("outliner_unlink_objects", [{
            "label": "unlink %s" % name,
            "apply": (lambda obj=obj: objects_rel(collection).unlink(obj)),
            "undo": (lambda obj=obj: objects_rel(collection).link(obj)),
        } for name, obj in pending])
        after_index = build_index(target_scene)
        after = {name: membership(obj, after_index) for name, obj in resolved}
        return write_result(
            target_scene, collection, resolved_path, after_index,
            unlinked=[name for name, _ in pending],
            linked=[],
            skipped=skipped,
            confirm_orphan=confirm_orphan,
            orphan_objects=blocking,
            planned=planned,
            changed=bool(pending),
            idempotent=not pending,
            before=before,
            after=after,
            warnings=warnings,
        )

    # ----------------------------------------------------------------- #
    # write: outliner_set_visibility
    # ----------------------------------------------------------------- #
    @api.command("outliner_set_visibility",
                 summary="set the native hide_viewport / hide_render flags of one collection")
    def set_visibility(scene, path, hide_viewport=None, hide_render=None):
        requested = {}
        for value, label in ((hide_viewport, "hide_viewport"), (hide_render, "hide_render")):
            if value is None:
                continue
            if type(value) is not bool:
                fail("invalid_argument", "%s must be a boolean or null, got %r" % (label, value))
            requested[label] = value
        if not requested:
            fail(
                "invalid_argument",
                "provide hide_viewport and/or hide_render; calling with both null would change nothing",
            )
        target_scene = scene_named(scene, write=True)
        index = index_for(target_scene, require_complete=True)
        collection, resolved_path = resolve(target_scene, path, allow_root=True)
        guard(target_scene, collection, index, "collection %r" % name_of(collection))
        for label in requested:
            if not hasattr(collection, label):
                fail(
                    "api_unsupported",
                    "this Blender build does not expose Collection.%s" % label,
                    details={"collection": name_of(collection), "property": label},
                )
        before = {label: bool(getattr(collection, label)) for label in requested}
        for label, value in requested.items():
            if before[label] == value:
                continue
            try:
                setattr(collection, label, value)
            except Exception as exc:  # noqa: BLE001
                restored = {}
                for applied_label in requested:
                    try:
                        setattr(collection, applied_label, before[applied_label])
                        restored[applied_label] = before[applied_label]
                    except Exception as undo_exc:  # noqa: BLE001
                        restored[applied_label] = "restore failed: %s" % undo_exc
                fail(
                    "outliner_write_failed",
                    "Blender refused to set Collection.%s: %s; the previous values were restored where possible"
                    % (label, exc),
                    details={"collection": name_of(collection), "property": label,
                             "before": before, "restored": restored},
                )
        after = {label: bool(getattr(collection, label)) for label in requested}
        wrong = sorted(label for label, value in requested.items() if after[label] != value)
        if wrong:
            fail(
                "outliner_write_failed",
                "Blender did not store the requested value for %s (read back %s)"
                % (", ".join(wrong), {label: after[label] for label in wrong}),
                details={"collection": name_of(collection), "before": before, "after": after,
                         "requested": requested},
            )
        changed = any(before[label] != after[label] for label in requested)
        return {
            "scene": name_of(target_scene),
            "path": resolved_path,
            "collection": row(collection, resolved_path, index),
            "requested": requested,
            "before": before,
            "after": after,
            "changed": changed,
            "idempotent": not changed,
            "objects_deleted": [],
            "collections_deleted": [],
            "warnings": [],
            "limits": LIMITS,
        }

    # Internal registration contract, shared by the separately registered
    # management module. These functions are never exposed as arbitrary MCP
    # callbacks; all public operations still use named bridge commands.
    return {
        name: value for name, value in locals().items()
        if name in {
            "fail", "key", "name_of", "linked", "scene_named", "parse_path",
            "boolean", "object_names", "object_named", "all_collections",
            "index_for", "build_index", "resolve", "guard", "owners_of",
            "member_of", "membership", "row", "run_batch", "children_of",
            "objects_of", "children_rel", "objects_rel", "master_of",
            "write_target", "move_objects", "ensure_path", "hierarchy",
        }
    }
