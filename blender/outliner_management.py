"""Native Outliner *management* commands: Collection lifecycle, ordinary Mesh/Empty
Object creation/rename/delete and the Outliner display switches.

This module is the second Outliner work package.  It is registered next to
``blender/outliner.py`` and publishes its bridge commands through
``register_commands(api, helpers)``.  It imports only ``bpy`` (plus the Python
standard library), never the ``mcp`` package, never ``bpy.context`` and never
``bpy.ops``, so a GUI session and ``--background`` behave identically.

Command set (nine named bridge commands, no dynamic callback)
-------------------------------------------------------------
``outliner_collection_create``, ``outliner_collection_rename``,
``outliner_collection_move``, ``outliner_collection_delete``,
``outliner_object_create``, ``outliner_object_rename``, ``outliner_object_delete``,
``outliner_collection_display``, ``outliner_object_display``.

Objects here are **ordinary Mesh/Empty objects** (``bpy.data.objects.new``), not
Collection Instances: nothing in this module creates or writes
``Object.instance_type`` / ``Object.instance_collection``.  Collection Instances
are only *read* by the delete safety scan, because a collection that is the source
of an instance is an external user of itself and is therefore never deleted.
Object reads/inspection stay with the existing core ``blender_object_info`` tool.

Helper reuse (no duplicate primitives)
--------------------------------------
Every collection/object primitive comes from the helper dict returned by
``blender/outliner.py``'s ``register_commands(api)``.  This module never
re-implements ``guard`` / ``resolve`` / ``run_batch`` / ``write_target`` and never
edits the owning file; if the helper dict is missing or incomplete it fails loudly
at registration time and names exactly what is missing.

Required helper keys::

    fail, key, name_of, linked, scene_named, parse_path, boolean, object_names,
    object_named, all_collections, index_for, build_index, resolve, guard,
    owners_of, member_of, membership, row, run_batch, children_of, objects_of,
    children_rel, objects_rel, master_of, write_target

(The owner module also publishes ``move_objects`` / ``ensure_path`` / ``hierarchy``;
this package does not need them - ordinary objects are moved by the existing
``outliner_move_objects`` command, so no object_move command is added here.)

Native API basis (Blender 5.2.1 LTS)
------------------------------------
* ``bpy.data.collections.new`` / ``CollectionChildren.link`` for collections;
  local names are unique per file and a colliding name would silently become
  ``name.001``, which this module never accepts (pre-checked file-wide + read back).
* ``bpy.data.meshes.new(name)`` + ``Mesh.from_pydata(vertices, [], faces)`` and
  ``bpy.data.objects.new(name, mesh_or_None)`` for ordinary objects;
  ``bpy.data.objects`` / ``bpy.data.meshes`` names are unique per ID type, so an
  Object and a Mesh may share one name while two Meshes may not - both collisions
  are pre-checked and refused instead of being auto-suffixed.
* ``bpy.data.objects.remove(object, do_unlink=True)`` and
  ``bpy.data.collections.remove(collection)`` free the datablock; afterwards its RNA
  is dead (``name`` / ``as_pointer()`` raise ``ReferenceError``).  Every removal path
  caches the names *before* the removal and never reads the freed struct again.
* Removing an Object does **not** free its data: the Mesh (and every material or
  Geometry Nodes group it uses) stays in ``bpy.data`` with zero users.  This module
  never purges orphan data.
* ``bpy.data.user_map(subset=[...])`` is the official "which IDs use this ID" view;
  combined with ``Object.users`` versus the object's direct collection ownership it
  proves whether an Object is referenced elsewhere (child objects, constraint
  targets, modifier objects, Geometry Nodes object sockets, drivers).  A reference
  that cannot be disproven refuses the deletion (``object_in_use``).
* Outliner display: the per-view-layer "eye" is ``LayerCollection.hide_viewport``
  for collections and ``Object.hide_set(hide, view_layer=...)`` /
  ``Object.hide_get(view_layer=...)`` for objects.  The "monitor" icon is the
  datablock-wide ``Collection.hide_viewport`` / ``Object.hide_viewport`` and render
  visibility is ``Collection.hide_render`` / ``Object.hide_render`` (a camera uses
  the same ``Object.hide_render``).  ``LayerCollection.exclude`` (the checkbox) is a
  separate per-view-layer flag.  ``LayerCollection`` nodes are matched from
  ``ViewLayer.layer_collection`` along the *collection path*, never from a UI
  context.

Safety contract
---------------
* Every command validates the whole request (scene, path, objects, target
  collection, view layer, geometry) before the first mutation, compares the state
  before and after, and reports ``warnings`` when a returned view may be incomplete.
* The scene root is immutable (``root_immutable``); a requested name that already
  exists is always refused (no ``.001``); the scene is never switched.
* Datablock-wide flags (``disable_viewport``, ``hide_render``) go through the shared
  ``guard`` (single scene, single parent, no library link); the per-view-layer flags
  additionally require the explicitly named view layer.
* ``outliner_collection_delete`` / ``outliner_object_delete`` are permanent and are
  **not** transactional: all referenced datablocks are pre-validated before
  deletion, but native removal can still fail partway through. A failure after the
  first real removal is reported as ``outliner_delete_partial`` with
  ``deleted`` / ``residual`` / ``partial: true`` / ``not_transactional: true``
  instead of pretending a rollback. The underlying mesh, material and Geometry
  Nodes datablocks are retained; no orphan data is purged. Object deletion removes
  only the explicitly named Object datablocks.
* Compensable steps (linking, unlinking, flag writes, collection creation) run
  through the shared ``run_batch`` primitives, which compensate in reverse order and
  report ``applied`` / ``compensated`` / ``residual``.  Creating one Mesh object is a
  single composite step: a failure removes exactly the datablocks this call created
  (reported as ``cleanup.removed`` / ``cleanup.residual``).
"""

from __future__ import annotations

import math

import bpy

# --------------------------------------------------------------------------- #
# bounds (every traversal, every payload and every geometry batch is finite)
# --------------------------------------------------------------------------- #
MAX_PATH_BYTES = 1024
MAX_PATH_SEGMENTS = 64
MAX_NAME_BYTES = 255
MAX_OBJECTS = 512
MAX_SUBTREE = 512
MAX_VIEW_LAYERS = 64
MAX_LAYER_NODES = 4000
MAX_OBJECT_ROWS = 512
MAX_VERTICES = 20000
MAX_FACES = 20000
MAX_FACE_CORNERS = 120000
MAX_NAME_SAMPLE = 20

LIMITS = {
    "path_bytes": MAX_PATH_BYTES,
    "path_segments": MAX_PATH_SEGMENTS,
    "name_bytes": MAX_NAME_BYTES,
    "objects_per_call": MAX_OBJECTS,
    "subtree_collections": MAX_SUBTREE,
    "view_layers_per_scene": MAX_VIEW_LAYERS,
    "layer_collection_nodes": MAX_LAYER_NODES,
    "object_rows": MAX_OBJECT_ROWS,
    "vertices_per_call": MAX_VERTICES,
    "faces_per_call": MAX_FACES,
    "face_corners_per_call": MAX_FACE_CORNERS,
    "name_sample": MAX_NAME_SAMPLE,
}

#: The nine bridge commands published by this module (no ``blender_`` prefix).
COMMAND_NAMES = (
    "outliner_collection_create",
    "outliner_collection_rename",
    "outliner_collection_move",
    "outliner_collection_delete",
    "outliner_object_create",
    "outliner_object_rename",
    "outliner_object_delete",
    "outliner_collection_display",
    "outliner_object_display",
)

#: Helper keys this module reads from ``blender/outliner.py``.
HELPER_NAMES = (
    "fail", "key", "name_of", "linked", "scene_named", "parse_path", "boolean",
    "object_names", "object_named", "all_collections", "index_for", "build_index",
    "resolve", "guard", "owners_of", "member_of", "membership", "row", "run_batch",
    "children_of", "objects_of", "children_rel", "objects_rel", "master_of",
    "write_target",
)

#: Object types this module is allowed to allocate.
SUPPORTED_OBJECT_TYPES = ("MESH", "EMPTY")

#: Display switches: ``label -> (attribute, qualified property, scope)``.
COLLECTION_VIEW_LAYER_FLAGS = {
    "hidden": ("hide_viewport", "LayerCollection.hide_viewport"),
    "exclude": ("exclude", "LayerCollection.exclude"),
}
COLLECTION_DATABLOCK_FLAGS = {
    "disable_viewport": ("hide_viewport", "Collection.hide_viewport"),
    "hide_render": ("hide_render", "Collection.hide_render"),
}
OBJECT_VIEW_LAYER_FLAGS = {
    "hidden": ("hide_set", "Object.hide_set(view_layer=...)"),
}
OBJECT_DATABLOCK_FLAGS = {
    "disable_viewport": ("hide_viewport", "Object.hide_viewport"),
    "hide_render": ("hide_render", "Object.hide_render"),
}


def _obtain_helpers(api, helpers):
    """Return the helper dict of ``blender/outliner.py``.

    ``helpers`` is what the owning module's ``register_commands(api)`` returns.  When
    it was not passed, the sibling module is asked for it once; the result must be a
    dict, otherwise the missing contract is reported instead of being worked around.
    """
    if isinstance(helpers, dict):
        return helpers
    try:
        import outliner as owner  # noqa: PLC0415 - resolved at registration time
    except ImportError as exc:  # pragma: no cover - only outside a Blender add-on
        raise RuntimeError(
            "blender/outliner_management.py cannot reuse the collection primitives: the "
            "sibling module blender/outliner.py is not importable (%s). Register this module "
            "with outliner_management.register_commands(api, helpers) and pass the helper "
            "dict returned by outliner.register_commands(api)." % exc
        ) from exc
    getter = getattr(owner, "register_commands", None)
    if not callable(getter):  # pragma: no cover - defensive
        raise RuntimeError(
            "blender/outliner.py exposes no register_commands(api), so its helper dict cannot "
            "be reused by blender/outliner_management.py."
        )
    shared = getter(api)
    if isinstance(shared, dict):
        return shared
    raise RuntimeError(
        "blender/outliner.py:register_commands(api) does not return its helper dict, so "
        "blender/outliner_management.py cannot reuse the collection primitives (it will not "
        "re-implement or modify them). Make outliner.register_commands(api) return its helper "
        "dict and register this module with outliner_management.register_commands(api, helpers)."
    )


def register_commands(api, helpers=None):
    error = api.CommandError
    shared = _obtain_helpers(api, helpers)
    missing = sorted(name for name in HELPER_NAMES if not callable(shared.get(name)))
    if missing:
        raise RuntimeError(
            "blender/outliner_management.py requires the helper key(s) %s from "
            "blender/outliner.py:register_commands(api); the supplied helper dict exposes %s. "
            "The owning module must publish them - this module never re-implements them."
            % (", ".join(missing), ", ".join(sorted(shared)) or "nothing")
        )

    fail = shared["fail"]
    key = shared["key"]
    name_of = shared["name_of"]
    linked = shared["linked"]
    scene_named = shared["scene_named"]
    parse_path = shared["parse_path"]
    boolean = shared["boolean"]
    object_names = shared["object_names"]
    object_named = shared["object_named"]
    all_collections = shared["all_collections"]
    index_for = shared["index_for"]
    build_index = shared["build_index"]
    resolve = shared["resolve"]
    guard = shared["guard"]
    owners_of = shared["owners_of"]
    member_of = shared["member_of"]
    membership = shared["membership"]
    row = shared["row"]
    run_batch = shared["run_batch"]
    children_of = shared["children_of"]
    objects_of = shared["objects_of"]
    children_rel = shared["children_rel"]
    objects_rel = shared["objects_rel"]
    master_of = shared["master_of"]
    # Part of the shared primitive contract; the nine commands here address objects by
    # name, so they never need this one themselves.
    write_target = shared["write_target"]

    # ----------------------------------------------------------------- #
    # local primitives (only the tiny helpers outliner.py does not publish)
    # ----------------------------------------------------------------- #
    def overridden(block):
        return getattr(block, "override_library", None) is not None

    def sample(values, limit=MAX_NAME_SAMPLE):
        return [name_of(value) for value in list(values)[:limit]]

    def all_objects():
        try:
            return list(bpy.data.objects)
        except Exception as exc:  # noqa: BLE001 - a broken build must be explicit
            fail("api_unsupported", "bpy.data.objects is not readable: %s" % exc)

    def mesh_registry():
        registry = getattr(bpy.data, "meshes", None)
        if registry is None or not callable(getattr(registry, "new", None)):
            fail(
                "api_unsupported",
                "bpy.data.meshes is not available in this Blender build, so no Mesh object can "
                "be created",
            )
        return registry

    def objects_named(value):
        return [block for block in all_objects() if name_of(block) == value]

    def meshes_named(value):
        return [block for block in list(mesh_registry()) if name_of(block) == value]

    def collections_named(value):
        return [block for block in all_collections() if name_of(block) == value]

    def require_name(value, label="name", single_segment=True):
        if not isinstance(value, str) or not value:
            fail("invalid_name", "%s must be a non-empty exact name" % label)
        if "\x00" in value or value != value.strip():
            fail("invalid_name", "%s must not contain NUL or surrounding whitespace" % label)
        if single_segment and ("/" in value or "\\" in value):
            fail(
                "invalid_name",
                "%s must not contain '/' or '\\': a collection name is a single path segment here, "
                "so such a name could never be addressed again" % label,
            )
        if len(value.encode("utf-8")) > MAX_NAME_BYTES:
            fail("invalid_name", "%s is longer than %d UTF-8 bytes" % (label, MAX_NAME_BYTES))
        return value

    def require_object_name(value, label="object"):
        if not isinstance(value, str) or not value:
            fail("invalid_objects", "%s must be a non-empty exact object name" % label)
        if "\x00" in value or value != value.strip():
            fail("invalid_objects", "%s must not contain NUL or surrounding whitespace" % label)
        if len(value.encode("utf-8")) > MAX_NAME_BYTES:
            fail("invalid_objects", "%s is longer than %d UTF-8 bytes" % (label, MAX_NAME_BYTES))
        return value

    def require_object_type(value):
        if not isinstance(value, str) or value not in SUPPORTED_OBJECT_TYPES:
            fail(
                "invalid_type",
                "type must be exactly 'MESH' or 'EMPTY', got %r"
                % (value if isinstance(value, str) else repr(value)),
                details={"type": value if isinstance(value, str) else repr(value),
                         "supported": list(SUPPORTED_OBJECT_TYPES)},
            )
        return value

    def scene_object(obj, scene, index, label):
        """Require an object explicitly linked in exactly this scene and writable."""
        state = membership(obj, index)
        # The compact report may truncate owners; safety checks every owner.
        state["scenes"] = sorted({scene_name for owner in owners_of(obj, index)
                                  for scene_name in index["in_scene"].get(key(owner), [])})
        if state["orphan"] or name_of(scene) not in state["scenes"]:
            fail(
                "object_not_in_scene",
                "%s is not linked into scene %r (it belongs to %s), so this write is refused"
                % (label, name_of(scene), ", ".join(state["scenes"]) or "no scene"),
                details={"object": name_of(obj), "scene": name_of(scene), "scenes": state["scenes"],
                         "membership": state},
            )
        if len(state["scenes"]) > 1:
            fail(
                "object_shared_across_scenes",
                "%s belongs to scenes %s, so a change here would change those scenes too"
                % (label, ", ".join(state["scenes"])),
                details={"object": name_of(obj), "scenes": state["scenes"], "membership": state},
            )
        if state["linked_object"] or state["override_object"]:
            fail(
                "read_only_object",
                "%s is linked from a library or carries a library override; it is never written" % label,
                details={"object": name_of(obj), "library": bool(getattr(obj, "library", None)),
                         "override": overridden(obj)},
            )
        return state

    def matrix_rows(value):
        if value is None:
            return None
        try:
            rows = []
            for entry in value:
                try:
                    rows.append([float(item) for item in entry])
                except TypeError:
                    rows.append(float(entry))
            return rows
        except (TypeError, ValueError):
            return None

    def reference_snapshot(obj):
        """Everything a rename must leave untouched (data, modifiers, GN, drivers, keys)."""
        data = getattr(obj, "data", None)
        animation = getattr(obj, "animation_data", None)
        drivers = getattr(animation, "drivers", None) if animation is not None else None
        shape_keys = getattr(data, "shape_keys", None) if data is not None else None
        key_blocks = getattr(shape_keys, "key_blocks", None) if shape_keys is not None else None
        modifiers = []
        for modifier in list(getattr(obj, "modifiers", []) or []):
            group = getattr(modifier, "node_group", None)
            modifiers.append({
                "type": getattr(modifier, "type", None),
                "name": name_of(modifier),
                "node_group": name_of(group),
            })
        return {
            "data": name_of(data) if data is not None else None,
            "data_id": key(data) if data is not None else None,
            "modifiers": modifiers,
            "modifier_count": len(modifiers),
            "node_groups": sorted({entry["node_group"] for entry in modifiers
                                   if entry["node_group"]}),
            "shape_keys": [name_of(block) for block in key_blocks] if key_blocks is not None else None,
            "driver_count": len(list(drivers)) if drivers is not None else None,
            "vertex_groups": [name_of(group) for group in list(getattr(obj, "vertex_groups", []) or [])],
            "constraints": [name_of(constraint) for constraint in list(getattr(obj, "constraints", []) or [])],
            "parent": name_of(getattr(obj, "parent", None)),
            "matrix_world": matrix_rows(getattr(obj, "matrix_world", None)),
        }

    def node_group_object_users(obj):
        """Geometry Nodes (or other node tree) object references to ``obj``."""
        found = []
        for group in list(getattr(bpy.data, "node_groups", []) or []):
            for node in list(getattr(group, "nodes", []) or []):
                if getattr(node, "object", None) is obj:
                    found.append({"user": name_of(group), "relationship": "geometry_nodes_object_reference",
                                  "node": name_of(node)})
                    break
        return found

    def object_relation_users(obj):
        """Child/constraint/modifier references from other objects to ``obj``."""
        found = []
        for other in all_objects():
            if other is obj:
                continue
            relationship = None
            if getattr(other, "parent", None) is obj:
                relationship = "child_object"
            else:
                for constraint in list(getattr(other, "constraints", []) or []):
                    if getattr(constraint, "target", None) is obj or getattr(constraint, "object", None) is obj:
                        relationship = "constraint_target"
                        break
                if relationship is None:
                    for modifier in list(getattr(other, "modifiers", []) or []):
                        if getattr(modifier, "object", None) is obj:
                            relationship = "modifier_object"
                            break
            if relationship is not None:
                found.append({"user": name_of(other), "relationship": relationship})
        return found

    def user_map_for(objects):
        """``bpy.data.user_map(subset=objects)`` when available, else ``None``."""
        user_map = getattr(bpy.data, "user_map", None)
        if not callable(user_map):
            return None
        try:
            mapping = user_map(subset=list(objects))
        except Exception:  # noqa: BLE001 - a build without the API is handled by the fallback
            return None
        return mapping if isinstance(mapping, dict) else None

    def mapped_users(mapping, obj):
        if mapping is None:
            return None
        listed = mapping.get(obj)
        if listed is None:
            for candidate, users in mapping.items():
                try:
                    if key(candidate) == key(obj):
                        listed = users
                        break
                except Exception:  # noqa: BLE001
                    continue
        if listed is None:
            return None
        try:
            return list(listed)
        except TypeError:
            return None

    def object_external_users(obj, index, mapping):
        """References that would break if the Object datablock were deleted.

        The decision combines the explicit object/constraint/modifier/node-tree
        relations with the official ``bpy.data.user_map`` view and, as the conservative
        backstop, ``Object.users`` versus the object's direct collection ownership.  A
        reference that cannot be disproven refuses the deletion.
        """
        owners = owners_of(obj, index)
        owner_keys = {key(owner) for owner in owners}
        owner_scenes = {name for owner in owners for name in index["in_scene"].get(key(owner), [])}
        structural_scenes = {key(scene): scene for scene in list(bpy.data.scenes)
                             if name_of(scene) in owner_scenes}
        entries = object_relation_users(obj)
        entries.extend(node_group_object_users(obj))
        mapped = mapped_users(mapping, obj)
        if mapped is not None:
            for user in mapped:
                if user is obj:
                    continue
                try:
                    if key(user) in owner_keys:
                        continue
                    if key(user) in structural_scenes:
                        # Blender's user_map includes owning Scenes through their
                        # embedded collections/view layers. These are structural
                        # users, not extra Object.users. Keep direct/weak Scene
                        # references protected before excluding that structural ID.
                        scene_refs = [getattr(user, "camera", None)]
                        scene_refs.extend(getattr(marker, "camera", None)
                                          for marker in list(getattr(user, "timeline_markers", []) or []))
                        animation = getattr(user, "animation_data", None)
                        for driver in list(getattr(animation, "drivers", []) or []):
                            for variable in list(getattr(getattr(driver, "driver", None), "variables", []) or []):
                                scene_refs.extend(getattr(target, "id", None)
                                                  for target in list(getattr(variable, "targets", []) or []))
                        for keying_set in list(getattr(user, "keying_sets", []) or []):
                            scene_refs.extend(getattr(path, "id", None)
                                              for path in list(getattr(keying_set, "paths", []) or []))
                        has_scene_ref = any(ref is not None and key(ref) == key(obj) for ref in scene_refs)
                        users = getattr(obj, "users", None)
                        if (not has_scene_ref and isinstance(users, int) and not isinstance(users, bool)
                                and users == len(owners)):
                            continue
                except Exception:  # noqa: BLE001
                    pass
                entries.append({"user": name_of(user), "relationship": "external_id_user",
                                "type": getattr(user, "type", None) or type(user).__name__})
        users = getattr(obj, "users", None)
        external_count = None
        if isinstance(users, int) and not isinstance(users, bool):
            external_count = max(0, users - len(owners))
            if external_count and not entries:
                entries.append({"user": "<unresolved>", "relationship": "users_count",
                                "count": external_count})
        if not entries:
            if mapped is None and external_count is None:
                return {"reason": "unknown_reference_state", "users": [], "user_count": None,
                        "owners": [name_of(owner) for owner in owners],
                        "direct_parent_collections": len(owners), "external_user_count": None}
            return None
        return {
            "reason": entries[0]["relationship"],
            "users": entries[:MAX_NAME_SAMPLE],
            "user_count": len(entries),
            "owners": [name_of(owner) for owner in owners],
            "direct_parent_collections": len(owners),
            "external_user_count": external_count,
        }

    def subtree_of(root, index):
        """``root`` plus every collection below it, breadth first, bounded."""
        found, seen = [], set()
        queue = [key(root)]
        while queue:
            node_key = queue.pop(0)
            if node_key in seen:
                continue
            seen.add(node_key)
            node = index["node_by_key"].get(node_key)
            if node is None:
                fail(
                    "index_truncated",
                    "collection %r is missing from the bounded graph index, so its subtree cannot "
                    "be proven and nothing was changed" % name_of(root),
                    details={"collection": name_of(root), "limits": LIMITS},
                )
            found.append(node)
            if len(found) > MAX_SUBTREE:
                fail(
                    "collection_tree_too_large",
                    "the subtree of %r exceeds the bound of %d collections" % (name_of(root), MAX_SUBTREE),
                    details={"collection": name_of(root), "count": len(found), "limits": LIMITS},
                )
            queue.extend(key(child) for child in index["children_map"].get(node_key, []))
        return found

    def descendant_or_self(candidate, root, index):
        """Whether ``candidate`` is ``root`` or lies below it (bounded, refuses truncation)."""
        target = key(candidate)
        seen, stack = set(), [key(root)]
        while stack:
            node_key = stack.pop()
            if node_key in seen:
                continue
            seen.add(node_key)
            if node_key == target:
                return True
            if len(seen) > MAX_SUBTREE:
                fail(
                    "index_truncated",
                    "the subtree of %r exceeds the bound of %d collections, so 'no cycle' cannot be "
                    "proven and nothing was changed" % (name_of(root), MAX_SUBTREE),
                    details={"collection": name_of(root), "limits": LIMITS},
                )
            for child in index["children_map"].get(node_key, []):
                stack.append(key(child))
        return False

    def subtree_depths(root_key, index, subtree_keys):
        """Depth of every collection *inside* the deletion subtree, counting from the root.

        Purely subtree-local, so a huge surrounding scene can never truncate the map
        and reorder the removal of children before their parents.
        """
        depths, queue = {root_key: 0}, [root_key]
        while queue:
            node_key = queue.pop(0)
            for child in index["children_map"].get(node_key, []):
                child_key = key(child)
                if child_key in subtree_keys and child_key not in depths:
                    depths[child_key] = depths[node_key] + 1
                    queue.append(child_key)
        return depths

    def node_group_users(collection):
        """Node groups whose nodes reference ``collection`` (e.g. a Collection Info node)."""
        found = []
        collection_key = key(collection)
        for group in list(getattr(bpy.data, "node_groups", []) or []):
            for node in list(getattr(group, "nodes", []) or []):
                referenced = getattr(node, "collection", None)
                if referenced is None:
                    continue
                try:
                    if key(referenced) == collection_key:
                        found.append(name_of(group))
                        break
                except Exception:  # noqa: BLE001 - a dead reference is not a live user
                    continue
        return found

    def instance_users(collection):
        """Objects that use ``collection`` as their Collection Instance source.

        Read-only safety scan: a Collection Instance keeps its source collection alive,
        so deleting that collection behind the instance's back is refused.
        """
        found = []
        collection_key = key(collection)
        for obj in all_objects():
            referenced = getattr(obj, "instance_collection", None)
            if referenced is None:
                continue
            try:
                if key(referenced) == collection_key:
                    found.append(name_of(obj))
            except Exception:  # noqa: BLE001
                continue
        return found

    def external_user_count(collection, parent_count):
        """``Collection.users`` beyond its direct parents, or ``None`` when unreadable."""
        users = getattr(collection, "users", None)
        if isinstance(users, bool) or not isinstance(users, int):
            return None
        return max(0, users - parent_count)

    # ----------------------------------------------------------------- #
    # view-layer primitives (never a UI context)
    # ----------------------------------------------------------------- #
    def view_layer_named(scene, value):
        if not isinstance(value, str) or not value:
            fail("invalid_view_layer", "view_layer must be a non-empty exact view-layer name")
        if "\x00" in value or value != value.strip():
            fail("invalid_view_layer", "view_layer must be an exact name without NUL or surrounding whitespace")
        if len(value.encode("utf-8")) > MAX_NAME_BYTES:
            fail("invalid_view_layer", "view_layer is longer than %d UTF-8 bytes" % MAX_NAME_BYTES)
        available = list(getattr(scene, "view_layers", []) or [])
        if len(available) > MAX_VIEW_LAYERS:
            fail(
                "index_truncated",
                "scene %r reports more than %d view layers" % (name_of(scene), MAX_VIEW_LAYERS),
                details={"view_layers": len(available), "limits": LIMITS},
            )
        matches = [layer for layer in available if name_of(layer) == value]
        if not matches:
            fail(
                "view_layer_not_found",
                "scene %r has no view layer named %r" % (name_of(scene), value),
                details={"scene": name_of(scene), "view_layer": value, "available": sample(available)},
            )
        if len(matches) > 1:
            fail(
                "ambiguous_view_layer",
                "%d view layers of scene %r are named %r; exact identity is required"
                % (len(matches), name_of(scene), value),
                details={"scene": name_of(scene), "view_layer": value, "matches": len(matches)},
            )
        return matches[0]

    def layer_collection_at(layer, segments, value, collection):
        """The ``LayerCollection`` addressed by ``value`` inside ``layer``.

        The walk starts at ``ViewLayer.layer_collection`` and follows the *collection
        path* segment by segment, then proves that the reached node indeed wraps the
        collection resolved through the scene DAG.  An excluded branch still has its
        ``LayerCollection`` node, so an excluded collection stays addressable here.
        """
        current = getattr(layer, "layer_collection", None)
        if current is None:
            fail(
                "api_unsupported",
                "view layer %r exposes no layer_collection, so per-view-layer display cannot be "
                "addressed" % name_of(layer),
                details={"view_layer": name_of(layer)},
            )
        walked, nodes = [], 0
        for segment in segments:
            nodes += 1
            if nodes > MAX_LAYER_NODES:
                fail(
                    "index_truncated",
                    "the layer-collection tree of view layer %r exceeds the bound of %d nodes"
                    % (name_of(layer), MAX_LAYER_NODES),
                    details={"view_layer": name_of(layer), "limits": LIMITS},
                )
            children = list(getattr(current, "children", []) or [])
            matches = [child for child in children
                       if name_of(getattr(child, "collection", None)) == segment]
            if len(matches) > 1:
                fail(
                    "ambiguous_collection",
                    "%d child layer collections of %r are named %r in view layer %r; the path is "
                    "ambiguous and nothing was changed"
                    % (len(matches), name_of(getattr(current, "collection", None)), segment,
                       name_of(layer)),
                    details={"path": value, "segment": segment, "view_layer": name_of(layer)},
                )
            if not matches:
                fail(
                    "collection_not_found",
                    "view layer %r has no collection named %r below %r"
                    % (name_of(layer), segment, name_of(getattr(current, "collection", None))),
                    details={
                        "path": value,
                        "segment": segment,
                        "view_layer": name_of(layer),
                        "resolved": "/".join(walked),
                        "available": sample([getattr(child, "collection", None) for child in children]),
                    },
                )
            current = matches[0]
            walked.append(segment)
        wrapped = getattr(current, "collection", None)
        if wrapped is None or key(wrapped) != key(collection):
            fail(
                "collection_not_in_view_layer",
                "the LayerCollection at %r does not wrap the addressed collection %r in view layer %r"
                % (value, name_of(collection), name_of(layer)),
                details={"path": value, "view_layer": name_of(layer), "collection": name_of(collection)},
            )
        return current

    def read_hidden(obj, layer):
        getter = getattr(obj, "hide_get", None)
        if not callable(getter):
            fail(
                "api_unsupported",
                "this Blender build does not expose Object.hide_get, so the per-view-layer hide "
                "state cannot be read back",
                details={"object": name_of(obj)},
            )
        try:
            return bool(getter(view_layer=layer))
        except TypeError:
            return bool(getter(layer))

    def write_hidden(obj, layer, value):
        setter = getattr(obj, "hide_set", None)
        if not callable(setter):
            fail(
                "api_unsupported",
                "this Blender build does not expose Object.hide_set, so the per-view-layer hide "
                "state cannot be written",
                details={"object": name_of(obj)},
            )
        try:
            setter(bool(value), view_layer=layer)
        except TypeError:
            setter(bool(value), layer)

    # ----------------------------------------------------------------- #
    # geometry validation for outliner_object_create
    # ----------------------------------------------------------------- #
    def geometry_plan(requested_type, vertices, faces):
        """Validated ``(vertices, faces)`` for a new mesh, or ``None`` for an Empty.

        Every check happens before the first ``bpy.data`` call, so a malformed request
        cannot leave a partially created mesh behind.
        """
        if requested_type == "EMPTY":
            if vertices is not None or faces is not None:
                fail(
                    "invalid_argument",
                    "type 'EMPTY' accepts no vertices/faces; create a 'MESH' to provide geometry",
                    details={
                        "type": "EMPTY",
                        "parameters": [label for label, value in (("vertices", vertices), ("faces", faces))
                                       if value is not None],
                        "supported": list(SUPPORTED_OBJECT_TYPES),
                    },
                )
            return None
        if vertices is None and faces is None:
            return {"vertices": [], "faces": [], "empty": True,
                    "note": "empty mesh data for a Geometry Nodes modifier"}
        if vertices is None:
            vertices = []
        if not isinstance(vertices, list):
            fail(
                "invalid_vertices",
                "vertices must be a list of [x, y, z] triples (or null for an empty mesh), got %r"
                % (vertices if isinstance(vertices, str) else type(vertices).__name__),
                details={"parameter": "vertices"},
            )
        if len(vertices) > MAX_VERTICES:
            fail(
                "invalid_vertices",
                "vertices accepts at most %d entries, got %d" % (MAX_VERTICES, len(vertices)),
                details={"count": len(vertices), "limits": LIMITS},
            )
        clean_vertices = []
        for position, vertex in enumerate(vertices):
            if not isinstance(vertex, list) or len(vertex) != 3:
                fail(
                    "invalid_vertices",
                    "vertices[%d] must be a list of exactly three numbers (x, y, z)" % position,
                    details={"index": position,
                             "entry": vertex if isinstance(vertex, list) else repr(vertex)},
                )
            triple = []
            for item in vertex:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    fail(
                        "invalid_vertices",
                        "vertices[%d] must contain only numbers, got %r" % (position, item),
                        details={"index": position, "entry": repr(item)},
                    )
                number = float(item)
                if not math.isfinite(number):
                    fail(
                        "invalid_vertices",
                        "vertices[%d] must be finite: NaN and infinity are refused" % position,
                        details={"index": position, "entry": repr(item)},
                    )
                triple.append(number)
            clean_vertices.append(triple)
        if faces is None:
            faces = []
        if not isinstance(faces, list):
            fail(
                "invalid_faces",
                "faces must be a list of vertex-index lists (or null), got %r"
                % (faces if isinstance(faces, str) else type(faces).__name__),
                details={"parameter": "faces"},
            )
        if len(faces) > MAX_FACES:
            fail(
                "invalid_faces",
                "faces accepts at most %d entries, got %d" % (MAX_FACES, len(faces)),
                details={"count": len(faces), "limits": LIMITS},
            )
        clean_faces, corners = [], 0
        for position, face in enumerate(faces):
            if not isinstance(face, list):
                fail(
                    "invalid_faces",
                    "faces[%d] must be a list of vertex indices, got %r"
                    % (position, face if isinstance(face, str) else type(face).__name__),
                    details={"index": position},
                )
            if len(face) < 3:
                fail(
                    "invalid_faces",
                    "faces[%d] needs at least three vertex indices, got %d" % (position, len(face)),
                    details={"index": position, "count": len(face)},
                )
            corners += len(face)
            if corners > MAX_FACE_CORNERS:
                fail(
                    "invalid_faces",
                    "the request exceeds %d total vertex indices across all faces" % MAX_FACE_CORNERS,
                    details={"corners": corners, "limits": LIMITS},
                )
            clean_face = []
            for slot, item in enumerate(face):
                if isinstance(item, bool) or not isinstance(item, int):
                    fail(
                        "invalid_faces",
                        "faces[%d][%d] must be an integer vertex index, got %r"
                        % (position, slot, item),
                        details={"index": position, "slot": slot, "entry": repr(item)},
                    )
                if item < 0 or item >= len(clean_vertices):
                    fail(
                        "invalid_faces",
                        "faces[%d][%d] = %d is not a valid index into the %d supplied vertices"
                        % (position, slot, item, len(clean_vertices)),
                        details={"index": position, "slot": slot, "vertex_index": item,
                                 "vertices": len(clean_vertices)},
                    )
                clean_face.append(item)
            clean_faces.append(clean_face)
        return {"vertices": clean_vertices, "faces": clean_faces,
                "empty": not clean_vertices and not clean_faces}

    # ----------------------------------------------------------------- #
    # creation/removal of datablocks owned by one call
    # ----------------------------------------------------------------- #
    def remove_created_object(obj):
        """Free an object this call created; the name is cached before the removal."""
        obj_name = name_of(obj)  # cached while the RNA is still valid
        record = {"object": obj_name, "removed": False, "error": None}
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            record["removed"] = True
            return record
        except Exception as first:  # noqa: BLE001 - retried once without do_unlink
            try:
                bpy.data.objects.remove(obj)
                record["removed"] = True
                return record
            except Exception as second:  # noqa: BLE001
                record["error"] = "%s: %s (do_unlink=True failed with %s: %s)" % (
                    type(second).__name__, second, type(first).__name__, first)
                return record

    def remove_created_mesh(mesh):
        """Free mesh data this call created; the name is cached before the removal."""
        mesh_name = name_of(mesh)  # cached while the RNA is still valid
        record = {"data": mesh_name, "removed": False, "error": None}
        try:
            mesh_registry().remove(mesh)
            record["removed"] = True
        except Exception as exc:  # noqa: BLE001
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
        return record

    def cleanup_created_geometry(obj, mesh):
        """Remove exactly the datablocks one failed create allocated.

        The object is freed first; the mesh data is only freed when the object really
        went away, so no half-deleted state (an object pointing at freed data) can be
        produced.  ``residual`` names what could not be cleaned.
        """
        object_name = name_of(obj) if obj is not None else None
        data_name = name_of(mesh) if mesh is not None else None
        record = {"object": object_name, "data": data_name, "removed": [], "residual": [],
                  "errors": [], "clean": True}
        # No object at all means the data block is already unreferenced by this call.
        object_gone = obj is None
        if obj is not None:
            outcome = remove_created_object(obj)
            if outcome["removed"]:
                object_gone = True
                record["removed"].append(object_name)
            else:
                record["errors"].append({"data_block": object_name, "error": outcome["error"]})
        if mesh is not None:
            if not object_gone:
                record["residual"].append(data_name)
                record["errors"].append({
                    "data_block": data_name,
                    "error": "still referenced by object %r, which could not be removed" % object_name,
                })
            else:
                outcome = remove_created_mesh(mesh)
                if outcome["removed"]:
                    record["removed"].append(data_name)
                else:
                    record["residual"].append(data_name)
                    record["errors"].append({"data_block": data_name, "error": outcome["error"]})
        record["clean"] = not record["residual"]
        return record

    def remove_created_collection(node):
        """Free a collection this call created; the name is cached before the removal."""
        node_name = name_of(node)  # cached while the RNA is still valid
        record = {"collection": node_name, "unlinked": False, "removed": False, "error": None}
        for candidate in list(all_collections()):
            if candidate is node:
                continue
            try:
                if any(child is node for child in children_of(candidate)):
                    children_rel(candidate).unlink(node)
                    record["unlinked"] = True
            except Exception:  # noqa: BLE001 - best effort, the removal is verified below
                continue
        try:
            bpy.data.collections.remove(node)
            record["removed"] = True
        except Exception as exc:  # noqa: BLE001
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
        return record

    def empty_result(scene_name, **extra):
        payload = {
            "scene": scene_name,
            "objects_deleted": [],
            "collections_deleted": [],
            "warnings": [],
            "limits": LIMITS,
        }
        payload.update(extra)
        return payload

    def reraisable(exc, cleanup):
        """Re-raise a failure with a cleanup record and honest details."""
        if isinstance(exc, error):
            details = dict(exc.details or {})
            details["cleanup"] = cleanup
            raise error(exc.code, exc.message, details=details) from exc
        fail(
            "outliner_write_failed",
            "the call failed after the datablock was allocated: %s" % exc,
            details={"error": "%s: %s" % (type(exc).__name__, exc), "cleanup": cleanup},
        )

    def child_path(parent_path, name):
        return name if not parent_path else "%s/%s" % (parent_path, name)

    def parent_path_of(path):
        return path.rsplit("/", 1)[0] if "/" in path else ""

    # ================================================================= #
    # 1. outliner_collection_create
    # ================================================================= #
    @api.command("outliner_collection_create",
                 summary="create one exactly named collection under an existing parent path")
    def collection_create(scene, parent_path, name):
        target = scene_named(scene, write=True)
        requested = require_name(name)
        index = index_for(target, require_complete=True)
        parent, parent_resolved = resolve(target, parent_path, allow_root=True, label="parent_path")
        guard(target, parent, index, "parent collection %r" % name_of(parent))
        resolved_path = child_path(parent_resolved, requested)

        existing = collections_named(requested)
        reused = None
        for candidate in existing:
            if any(child is candidate for child in children_of(parent)):
                reused = candidate
                break
        if reused is not None:
            return empty_result(
                name_of(target),
                parent_path=parent_resolved,
                path=resolved_path,
                name=requested,
                created=[],
                reused=[requested],
                changed=False,
                idempotent=True,
                prechecked=True,
                auto_suffix=False,
                collection=row(reused, resolved_path, index),
                parent=row(parent, parent_resolved, index),
            )
        if existing:
            fail(
                "collection_name_conflict",
                "a collection named %r already exists elsewhere in this file, so creating a second "
                "one here would make Blender rename it to %r.001 - which this server never does"
                % (requested, requested),
                details={
                    "parent_path": parent_resolved,
                    "path": resolved_path,
                    "requested_name": requested,
                    "reason": "exists_elsewhere",
                    "existing": [
                        {
                            "collection": name_of(candidate),
                            "scenes": sorted(index["in_scene"].get(key(candidate), [])),
                            "parents": sample(index["parent_map"].get(key(candidate), [])),
                            "linked": linked(candidate),
                        }
                        for candidate in existing[:MAX_NAME_SAMPLE]
                    ],
                    "auto_suffix": False,
                    "created": [],
                    "collections_deleted": [],
                },
            )

        before_children = [name_of(child) for child in children_of(parent)]
        try:
            node = bpy.data.collections.new(requested)
        except Exception as exc:  # noqa: BLE001
            fail(
                "outliner_write_failed",
                "Blender refused to allocate a collection named %r: %s" % (requested, exc),
                details={"parent_path": parent_resolved, "path": resolved_path, "requested_name": requested,
                         "created": [], "auto_suffix": False},
            )
        stored = name_of(node)  # cached before anything can free the datablock
        if stored != requested:
            cleanup = remove_created_collection(node)
            fail(
                "invalid_name",
                "Blender stored the new collection as %r instead of %r; the created datablock was "
                "removed again and no .001 name is accepted" % (stored, requested),
                details={"parent_path": parent_resolved, "path": resolved_path, "requested_name": requested,
                         "stored_name": stored, "cleanup": cleanup, "auto_suffix": False, "created": []},
            )
        try:
            run_batch("outliner_collection_create", [{
                "label": "link %s under %s" % (requested, name_of(parent)),
                "apply": lambda: children_rel(parent).link(node),
                "undo": lambda: children_rel(parent).unlink(node),
            }])
        except Exception as exc:  # noqa: BLE001 - the datablock is ours and must not survive
            reraisable(exc, remove_created_collection(node))
        if not any(child is node for child in children_of(parent)):
            reraisable(
                RuntimeError("the new collection is not linked under %r after the write" % name_of(parent)),
                remove_created_collection(node),
            )
        after_index = build_index(target)
        return empty_result(
            name_of(target),
            parent_path=parent_resolved,
            path=resolved_path,
            name=requested,
            created=[requested],
            reused=[],
            changed=True,
            idempotent=False,
            prechecked=True,
            auto_suffix=False,
            parent=row(parent, parent_resolved, after_index),
            collection=row(node, resolved_path, after_index),
            parent_children_before=before_children,
            parent_children_after=[name_of(child) for child in children_of(parent)],
        )

    # ================================================================= #
    # 2. outliner_collection_rename
    # ================================================================= #
    @api.command("outliner_collection_rename",
                 summary="rename one collection to an exact name; no automatic .001 suffix")
    def collection_rename(scene, path, name):
        target = scene_named(scene, write=True)
        requested = require_name(name)
        index = index_for(target, require_complete=True)
        collection, resolved = resolve(target, path, allow_root=True)
        if resolved == "":
            fail(
                "root_immutable",
                "the scene root collection %r cannot be renamed" % name_of(collection),
                details={"scene": name_of(target), "path": "", "reason": "root"},
            )
        guard(target, collection, index, "collection %r" % name_of(collection))
        current = name_of(collection)
        new_path = child_path(parent_path_of(resolved), requested)
        if current == requested:
            return empty_result(
                name_of(target), path=resolved, new_path=resolved, previous_name=current, name=requested,
                changed=False, idempotent=True, auto_suffix=False,
                collection=row(collection, resolved, index),
            )
        conflicts = [candidate for candidate in collections_named(requested) if candidate is not collection]
        if conflicts:
            fail(
                "collection_name_conflict",
                "a collection named %r already exists elsewhere in this file, so renaming %r to it "
                "would make Blender store %r.001 - which this server never does"
                % (requested, current, requested),
                details={
                    "path": resolved,
                    "previous_name": current,
                    "requested_name": requested,
                    "reason": "exists_elsewhere",
                    "existing": [
                        {
                            "collection": name_of(candidate),
                            "scenes": sorted(index["in_scene"].get(key(candidate), [])),
                            "parents": sample(index["parent_map"].get(key(candidate), [])),
                            "linked": linked(candidate),
                        }
                        for candidate in conflicts[:MAX_NAME_SAMPLE]
                    ],
                    "auto_suffix": False,
                    "changed": False,
                },
            )
        try:
            collection.name = requested
        except Exception as exc:  # noqa: BLE001
            fail(
                "outliner_write_failed",
                "Blender refused to rename %r to %r: %s" % (current, requested, exc),
                details={"path": resolved, "previous_name": current, "requested_name": requested},
            )
        stored = name_of(collection)
        if stored != requested:
            restored = current
            restore_error = None
            try:
                collection.name = current
            except Exception as exc:  # noqa: BLE001
                restored = None
                restore_error = "%s: %s" % (type(exc).__name__, exc)
            fail(
                "invalid_name",
                "Blender stored the collection as %r instead of %r; the previous name was restored "
                "where possible and no .001 name is accepted" % (stored, requested),
                details={"path": resolved, "previous_name": current, "requested_name": requested,
                         "stored_name": stored, "restored_name": restored, "restore_error": restore_error,
                         "auto_suffix": False},
            )
        after_index = build_index(target)
        return empty_result(
            name_of(target), path=resolved, new_path=new_path, previous_name=current, name=requested,
            changed=True, idempotent=False, auto_suffix=False,
            collection=row(collection, new_path, after_index),
        )

    # ================================================================= #
    # 3. outliner_collection_move
    # ================================================================= #
    @api.command("outliner_collection_move",
                 summary="move one collection under another existing parent collection")
    def collection_move(scene, path, parent_path):
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        source, source_resolved = resolve(target, path, allow_root=True)
        if source_resolved == "":
            fail(
                "root_immutable",
                "the scene root collection %r cannot be moved" % name_of(source),
                details={"scene": name_of(target), "path": "", "reason": "root"},
            )
        guard(target, source, index, "source collection %r" % name_of(source))
        parent, parent_resolved = resolve(target, parent_path, allow_root=True, label="parent_path")
        guard(target, parent, index, "target parent collection %r" % name_of(parent))
        if key(parent) == key(source):
            fail(
                "cycle_hierarchy",
                "moving %r under itself would make it its own parent" % name_of(source),
                details={"path": source_resolved, "parent_path": parent_resolved, "reason": "self"},
            )
        if descendant_or_self(parent, source, index):
            fail(
                "cycle_hierarchy",
                "the target parent %r lies inside the subtree of %r, so the move would create a "
                "hierarchy cycle" % (name_of(parent), name_of(source)),
                details={"path": source_resolved, "parent_path": parent_resolved,
                         "target_parent": name_of(parent), "reason": "descendant"},
            )
        parents = index["parent_map"].get(key(source), [])
        if not parents:
            fail(
                "collection_outside_scene",
                "collection %r has no direct parent collection, so it cannot be moved" % name_of(source),
                details={"path": source_resolved, "scene": name_of(target)},
            )
        current_parent = parents[0]
        new_path = child_path(parent_resolved, name_of(source))
        if key(current_parent) == key(parent):
            return empty_result(
                name_of(target), path=source_resolved, new_path=source_resolved,
                parent_path=parent_resolved, previous_parent=name_of(current_parent),
                changed=False, idempotent=True,
                collection=row(source, source_resolved, index),
            )
        # Link the target parent first and only then unlink the previous one.  When the
        # target parent is an *ancestor* of the source that order would temporarily
        # register the source twice on the same route (a diamond), so that single case
        # unlinks first and re-links in the compensation; both orders are compensated
        # in reverse and reported honestly.
        target_is_ancestor = descendant_or_self(source, parent, index)
        link_step = {
            "label": "link %s under %s" % (name_of(source), name_of(parent)),
            "apply": lambda: children_rel(parent).link(source),
            "undo": lambda: children_rel(parent).unlink(source),
        }
        unlink_step = {
            "label": "unlink %s from %s" % (name_of(source), name_of(current_parent)),
            "apply": lambda: children_rel(current_parent).unlink(source),
            "undo": lambda: children_rel(current_parent).link(source),
        }
        steps = [unlink_step, link_step] if target_is_ancestor else [link_step, unlink_step]
        run_batch("outliner_collection_move", steps)
        after_index = build_index(target)
        linked_to_parent = any(child is source for child in children_of(parent))
        still_at_parent = any(child is source for child in children_of(current_parent))
        if not linked_to_parent or still_at_parent:
            fail(
                "outliner_write_failed",
                "the move did not settle: linked_under_target=%s still_at_previous_parent=%s"
                % (linked_to_parent, still_at_parent),
                details={"path": source_resolved, "new_path": new_path, "linked": linked_to_parent,
                         "still_at_previous_parent": still_at_parent},
            )
        return empty_result(
            name_of(target), path=source_resolved, new_path=new_path, parent_path=parent_resolved,
            previous_parent=name_of(current_parent), parent=name_of(parent),
            changed=True, idempotent=False,
            collection=row(source, new_path, after_index),
            objects_preserved=sorted(name_of(obj) for obj in objects_of(source))[:MAX_OBJECT_ROWS],
        )

    # ================================================================= #
    # 4. outliner_collection_delete
    # ================================================================= #
    @api.command("outliner_collection_delete",
                 summary="delete exactly one collection (or its whole subtree) and never an object")
    def collection_delete(scene, path, confirm_delete, recursive=False):
        boolean(confirm_delete, "confirm_delete")
        boolean(recursive, "recursive")
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        collection, resolved = resolve(target, path, allow_root=True)
        if resolved == "":
            fail(
                "root_immutable",
                "the scene root collection %r cannot be deleted" % name_of(collection),
                details={"scene": name_of(target), "path": "", "reason": "root"},
            )
        if not confirm_delete:
            fail(
                "confirmation_required",
                "deleting collection %r is permanent and requires confirm_delete=true; nothing was "
                "changed" % name_of(collection),
                details={"scene": name_of(target), "path": resolved, "recursive": recursive,
                         "confirm_delete": False, "collections_deleted": [], "objects_deleted": []},
            )
        guard(target, collection, index, "collection %r" % name_of(collection))
        children = children_of(collection)
        if children and not recursive:
            fail(
                "collection_has_children",
                "collection %r still has %d child collection(s) (%s); pass recursive=true to delete "
                "exactly that subtree, or delete the children first - nothing was changed"
                % (name_of(collection), len(children), ", ".join(sample(children))),
                details={"scene": name_of(target), "path": resolved, "children": sample(children),
                         "child_count": len(children), "recursive": False,
                         "collections_deleted": [], "objects_deleted": []},
            )

        scene_name = name_of(target)
        subtree = subtree_of(collection, index)
        subtree_keys = {key(node) for node in subtree}
        names = {key(node): name_of(node) for node in subtree}

        # ---- phase 1: read-only prevalidation of the entire subtree ----
        problems = []
        for node in subtree:
            node_key = key(node)
            node_label = names[node_key]
            if linked(node):
                problems.append({
                    "code": "read_only_collection", "collection": node_label, "reason": "linked_or_override",
                    "message": "collection %r is linked/overridden and is never deleted" % node_label,
                })
            node_scenes = sorted(index["in_scene"].get(node_key, []))
            if scene_name not in node_scenes:
                problems.append({
                    "code": "collection_outside_scene", "collection": node_label, "reason": "outside_scene",
                    "message": "collection %r is not part of scene %r (it belongs to %s)"
                               % (node_label, scene_name, ", ".join(node_scenes) or "no scene"),
                })
            elif len(node_scenes) > 1:
                problems.append({
                    "code": "collection_shared_across_scenes", "collection": node_label,
                    "reason": "cross_scene", "scenes": node_scenes,
                    "message": "collection %r is part of scenes %s; deleting it here would delete it there too"
                               % (node_label, ", ".join(node_scenes)),
                })
            parents = index["parent_map"].get(node_key, [])
            if len(parents) > 1:
                problems.append({
                    "code": "collection_multi_parent", "collection": node_label, "reason": "multi_parent",
                    "parents": sample(parents),
                    "message": "collection %r has %d direct parent collections (%s), so it is not singly owned"
                               % (node_label, len(parents), ", ".join(sample(parents))),
                })
            for parent in parents:
                if key(parent) not in subtree_keys and node is not collection:
                    problems.append({
                        "code": "collection_in_use", "collection": node_label, "reason": "external_parent",
                        "parent": name_of(parent),
                        "message": "collection %r is also linked under %r outside the subtree, so deleting "
                                   "the subtree would break that route" % (node_label, name_of(parent)),
                    })
            if bool(getattr(node, "use_fake_user", False)):
                problems.append({
                    "code": "collection_in_use", "collection": node_label, "reason": "fake_user",
                    "message": "collection %r carries a fake user, so it is an external user of itself"
                               % node_label,
                })
            sources = instance_users(node)
            if sources:
                problems.append({
                    "code": "collection_in_use", "collection": node_label,
                    "reason": "collection_instance_source", "objects": sources[:MAX_NAME_SAMPLE],
                    "message": "collection %r is the source of the Collection Instance(s) %s; delete those "
                               "instances first" % (node_label, ", ".join(sources[:MAX_NAME_SAMPLE])),
                })
            groups = node_group_users(node)
            if groups:
                problems.append({
                    "code": "collection_in_use", "collection": node_label,
                    "reason": "geometry_nodes_reference", "node_groups": groups[:MAX_NAME_SAMPLE],
                    "message": "collection %r is referenced by the Geometry Nodes group(s) %s and is never "
                               "deleted behind their back" % (node_label, ", ".join(groups[:MAX_NAME_SAMPLE])),
                })
            external = external_user_count(node, len(parents))
            if external:
                problems.append({
                    "code": "collection_in_use", "collection": node_label, "reason": "external_users",
                    "users": getattr(node, "users", None), "direct_parents": len(parents),
                    "message": "collection %r reports %s user(s) beyond its %d direct parent collection(s), "
                               "so it is referenced from outside this subtree"
                               % (node_label, external, len(parents)),
                })
        if problems:
            first = problems[0]
            fail(
                first["code"],
                first["message"],
                details={"scene": scene_name, "path": resolved, "collection": name_of(collection),
                         "recursive": recursive, "problems": problems, "problem_count": len(problems),
                         "subtree": [names[key(node)] for node in subtree],
                         "collections_deleted": [], "objects_deleted": []},
            )

        # ---- phase 2: plan object preservation (no writes yet) ----
        # Every object is planned exactly once, even when it is linked directly in
        # several collections of the subtree; otherwise the pre-link step would try to
        # link it to the scene root twice.
        root = master_of(target)
        preserved, relink, unique_objects = [], [], []
        seen_objects = set()
        for node in subtree:
            for obj in objects_of(node):
                obj_key = key(obj)
                if obj_key in seen_objects:
                    continue
                seen_objects.add(obj_key)
                unique_objects.append(obj)
                obj_name = name_of(obj)
                remaining = [owner for owner in owners_of(obj, index) if key(owner) not in subtree_keys]
                scenes = sorted({scene_label for owner in remaining
                                 for scene_label in index["in_scene"].get(key(owner), [])})
                detaches = scene_name not in scenes
                if detaches and not member_of(root, obj, index):
                    if linked(obj) or overridden(obj):
                        fail(
                            "read_only_object",
                            "deleting the subtree would detach %r from scene %r and it is linked/overridden, "
                            "so it cannot be re-linked to the scene root; nothing was changed"
                            % (obj_name, scene_name),
                            details={"object": obj_name, "scene": scene_name,
                                     "path": resolved, "collections_deleted": [], "objects_deleted": []},
                        )
                    relink.append((obj_name, obj))
                preserved.append({
                    "object": obj_name,
                    "type": getattr(obj, "type", None),
                    "collections": [name_of(owner) for owner in remaining],
                    "scenes": scenes,
                    "relinked_to_root": detaches,
                    "orphan_after_delete": not remaining,
                })
        preserved.sort(key=lambda entry: entry["object"])
        relink.sort(key=lambda entry: entry[0])
        before = {name_of(obj): membership(obj, index) for obj in unique_objects}

        # ---- phase 3: compensable pre-link of objects that would leave the scene ----
        if relink:
            run_batch("outliner_collection_delete", [{
                "label": "link %s to the scene root" % obj_name,
                "apply": (lambda obj=obj: objects_rel(root).link(obj)),
                "undo": (lambda obj=obj: objects_rel(root).unlink(obj)),
            } for obj_name, obj in relink])

        # ---- phase 4: real removal (NOT compensable - reported honestly) ----
        # Every name and identity is cached BEFORE the first removal: after
        # ``bpy.data.collections.remove`` the RNA is dead and reading ``name`` or
        # ``as_pointer()`` raises ReferenceError.
        collection_key = key(collection)
        cached_subtree = [
            {"key": key(node), "name": names[key(node)],
             "parents": sample(index["parent_map"].get(key(node), []))}
            for node in subtree
        ]
        parent_names = [{"collection": entry["name"], "parents": entry["parents"]}
                        for entry in cached_subtree]
        depths = subtree_depths(collection_key, index, subtree_keys)
        order = sorted(range(len(cached_subtree)),
                       key=lambda position: depths.get(cached_subtree[position]["key"], 0),
                       reverse=True)
        outside_parents = [parent for parent in index["parent_map"].get(collection_key, [])
                           if key(parent) not in subtree_keys]
        deleted, current_label = [], names[collection_key]
        try:
            for position in order:
                entry = cached_subtree[position]
                current_label = entry["name"]
                node = subtree[position]
                for parent in index["parent_map"].get(entry["key"], []):
                    try:
                        children_rel(parent).unlink(node)
                    except Exception:  # noqa: BLE001 - the removal below is verified instead
                        continue
                bpy.data.collections.remove(node)
                deleted.append(entry["name"])
        except Exception as exc:  # noqa: BLE001 - a freed datablock has no readable RNA
            live_keys = {key(block) for block in all_collections()}
            residual = [entry["name"] for entry in cached_subtree if entry["key"] in live_keys]
            fail(
                "outliner_delete_partial",
                "the collection(s) %s were already removed and cannot be restored by this server; the "
                "deletion stopped at %r: %s. This command is not transactional - the applied removals "
                "remain and are reported instead of being described as a rollback."
                % (", ".join(deleted) or "none", current_label, exc),
                details={
                    "scene": scene_name, "path": resolved, "recursive": recursive,
                    "deleted": deleted, "failed": current_label,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "remaining": residual, "residual": residual, "partial": True,
                    "not_transactional": True, "rolled_back": False,
                    "objects_deleted": [], "objects_preserved": [entry["object"] for entry in preserved],
                },
            )
        live_keys = {key(block) for block in all_collections()}
        still_present = [entry["name"] for entry in cached_subtree if entry["key"] in live_keys]
        if still_present or len(deleted) != len(subtree):
            fail(
                "outliner_delete_partial",
                "Blender did not remove %s; this command is not transactional and reports the applied "
                "removals honestly" % ", ".join(still_present or ["<unknown>"]),
                details={"scene": scene_name, "path": resolved, "deleted": deleted,
                         "remaining": still_present, "residual": still_present, "partial": True,
                         "not_transactional": True, "rolled_back": False},
            )
        after_index = build_index(target)
        after = {name_of(obj): membership(obj, after_index) for obj in unique_objects}
        siblings = []
        for parent in outside_parents:
            siblings.extend(name_of(child) for child in children_of(parent)
                            if key(child) not in subtree_keys)
        warnings = [
            "collection removal is permanent in Blender: this command does not roll back a partially "
            "completed deletion and never deletes an object datablock",
        ]
        if relink:
            warnings.append(
                "%s were linked to the scene root because deleting %s would have detached them from "
                "scene %r; their datablocks are preserved"
                % (", ".join(entry[0] for entry in relink), resolved, scene_name)
            )
        if preserved:
            warnings.append(
                "%d object datablock(s) under the deleted subtree are preserved; no object, mesh, "
                "material or Geometry Nodes group was deleted" % len(preserved)
            )
        return {
            "scene": scene_name,
            "path": resolved,
            "recursive": recursive,
            "collection": names[collection_key],
            "subtree": [entry["name"] for entry in cached_subtree],
            "collections_deleted": deleted,
            "collection_count": len(deleted),
            "objects_deleted": [],
            "objects_preserved": [entry["object"] for entry in preserved],
            "preserved": preserved,
            "relinked_to_root": [entry[0] for entry in relink],
            "detached_objects": [entry["object"] for entry in preserved if entry["orphan_after_delete"]],
            "original_membership": before,
            "membership_after": after,
            "parent": name_of(outside_parents[0]) if outside_parents else None,
            "parent_names": parent_names,
            "siblings_kept": sorted(siblings),
            "partial": False,
            "not_transactional": True,
            "rolled_back": False,
            "changed": True,
            "idempotent": False,
            "warnings": warnings,
            "limits": LIMITS,
        }

    # ================================================================= #
    # 5. outliner_object_create
    # ================================================================= #
    @api.command("outliner_object_create",
                 summary="create one exact ordinary Mesh/Empty object inside a collection")
    def object_create(scene, path, name, type="MESH", vertices=None, faces=None):
        requested_type = require_object_type(type)
        requested_name = require_name(name, "name", single_segment=False)
        geometry = geometry_plan(requested_type, vertices, faces)
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        collection, resolved = resolve(target, path, allow_root=True)
        guard(target, collection, index, "target collection %r" % name_of(collection))

        conflicts = objects_named(requested_name)
        if conflicts:
            fail(
                "object_name_conflict",
                "an object named %r already exists in this file, so creating a second one here would "
                "make Blender rename it to %r.001 - which this server never does"
                % (requested_name, requested_name),
                details={
                    "path": resolved,
                    "requested_name": requested_name,
                    "reason": "exists_elsewhere",
                    "existing": [
                        {"object": name_of(candidate), "type": getattr(candidate, "type", None),
                         "linked": linked(candidate)}
                        for candidate in conflicts[:MAX_NAME_SAMPLE]
                    ],
                    "auto_suffix": False,
                    "objects_deleted": [],
                },
            )
        if requested_type == "MESH":
            data_conflicts = meshes_named(requested_name)
            if data_conflicts:
                fail(
                    "data_name_conflict",
                    "mesh data named %r already exists in this file, so the new mesh would be stored as "
                    "%r.001 - which this server never does. Object and mesh names live in different ID "
                    "namespaces, so rename the existing mesh or pick another name"
                    % (requested_name, requested_name),
                    details={
                        "path": resolved,
                        "requested_name": requested_name,
                        "reason": "mesh_name_exists_elsewhere",
                        "existing": [name_of(candidate) for candidate in data_conflicts[:MAX_NAME_SAMPLE]],
                        "auto_suffix": False,
                        "objects_deleted": [],
                    },
                )

        record = {"object": None, "mesh": None}

        def apply_create():
            mesh = None
            if requested_type == "MESH":
                try:
                    mesh = mesh_registry().new(requested_name)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError("Blender refused to allocate mesh data %r: %s"
                                       % (requested_name, exc)) from exc
                record["mesh"] = mesh
                stored_mesh = name_of(mesh)  # cached while the RNA is valid
                if stored_mesh != requested_name:
                    raise error(
                        "invalid_name",
                        "Blender stored the new mesh data as %r instead of %r" % (stored_mesh, requested_name),
                        details={"requested_name": requested_name, "stored_name": stored_mesh,
                                 "auto_suffix": False},
                    )
                if not geometry["empty"]:
                    try:
                        mesh.from_pydata(geometry["vertices"], [], geometry["faces"])
                        mesh.update()
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(
                            "Mesh.from_pydata refused the validated geometry (%d vertices, %d faces): %s"
                            % (len(geometry["vertices"]), len(geometry["faces"]), exc)
                        ) from exc
            try:
                obj = bpy.data.objects.new(requested_name, mesh)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("Blender refused to allocate an object named %r: %s"
                                   % (requested_name, exc)) from exc
            record["object"] = obj
            stored_object = name_of(obj)
            if stored_object != requested_name:
                raise error(
                    "invalid_name",
                    "Blender stored the new object as %r instead of %r" % (stored_object, requested_name),
                    details={"requested_name": requested_name, "stored_name": stored_object,
                             "auto_suffix": False},
                )
            expected = requested_type
            if getattr(obj, "type", None) != expected:
                raise RuntimeError(
                    "Blender created an object of type %r instead of %r"
                    % (getattr(obj, "type", None), expected)
                )
            objects_rel(collection).link(obj)
            if not member_of(collection, obj):
                raise RuntimeError("the new object is not linked into the target collection")

        try:
            apply_create()
        except Exception as exc:  # noqa: BLE001 - only the datablocks this call created are removed
            reraisable(exc, cleanup_created_geometry(record.get("object"), record.get("mesh")))

        created = record["object"]
        mesh = record["mesh"]
        after_index = build_index(target)
        warnings = []
        if requested_type == "MESH" and geometry["empty"]:
            warnings.append(
                "an empty MESH data block was created (no vertices/faces): it carries no geometry until a "
                "Geometry Nodes modifier generates some"
            )
        return empty_result(
            name_of(target),
            path=resolved,
            name=requested_name,
            type=getattr(created, "type", None),
            object={
                "name": name_of(created),
                "id": key(created),
                "type": getattr(created, "type", None),
                "data": name_of(mesh) if mesh is not None else None,
                "data_id": key(mesh) if mesh is not None else None,
                "data_type": "MESH" if mesh is not None else None,
                "vertices": len(getattr(mesh, "vertices", []) or []) if mesh is not None else None,
                "polygons": len(getattr(mesh, "polygons", []) or []) if mesh is not None else None,
                "linked": linked(created),
                "override": overridden(created),
            },
            data=name_of(mesh) if mesh is not None else None,
            vertices=len(getattr(mesh, "vertices", []) or []) if mesh is not None else 0,
            faces=len(getattr(mesh, "polygons", []) or []) if mesh is not None else 0,
            membership=membership(created, after_index),
            collection=row(collection, resolved, after_index),
            created=True,
            changed=True,
            idempotent=False,
            auto_suffix=False,
            cleanup=None,
            warnings=warnings,
        )

    # ================================================================= #
    # 6. outliner_object_rename
    # ================================================================= #
    @api.command("outliner_object_rename",
                 summary="rename one exact object to an exact name; no automatic .001 suffix")
    def object_rename(scene, object, name):
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        requested_object = require_object_name(object)
        obj = object_named(requested_object)
        state = scene_object(obj, target, index, "object %r" % requested_object)
        requested = require_name(name, "name", single_segment=False)
        current = name_of(obj)
        before = reference_snapshot(obj)
        if current == requested:
            return empty_result(
                name_of(target), object=current, previous_name=current, name=requested,
                changed=False, idempotent=True, auto_suffix=False,
                references=before, references_preserved=True, membership=state,
            )
        conflicts = [candidate for candidate in objects_named(requested) if candidate is not obj]
        if conflicts:
            fail(
                "object_name_conflict",
                "an object named %r already exists in this file, so renaming %r to it would make "
                "Blender store %r.001 - which this server never does" % (requested, current, requested),
                details={"object": current, "requested_name": requested, "reason": "exists_elsewhere",
                         "existing": [name_of(candidate) for candidate in conflicts[:MAX_NAME_SAMPLE]],
                         "auto_suffix": False, "changed": False},
            )
        try:
            obj.name = requested
        except Exception as exc:  # noqa: BLE001
            fail(
                "outliner_write_failed",
                "Blender refused to rename object %r to %r: %s" % (current, requested, exc),
                details={"object": current, "requested_name": requested, "references": before},
            )
        stored = name_of(obj)
        if stored != requested:
            restored, restore_error = current, None
            try:
                obj.name = current
            except Exception as exc:  # noqa: BLE001
                restored, restore_error = None, "%s: %s" % (type(exc).__name__, exc)
            fail(
                "invalid_name",
                "Blender stored the object as %r instead of %r; the previous name was restored where "
                "possible and no .001 name is accepted" % (stored, requested),
                details={"object": current, "requested_name": requested, "stored_name": stored,
                         "restored_name": restored, "restore_error": restore_error,
                         "auto_suffix": False, "references": before},
            )
        after = reference_snapshot(obj)
        if after != before:
            restored, restore_error = current, None
            try:
                obj.name = current
            except Exception as exc:  # noqa: BLE001
                restored, restore_error = None, "%s: %s" % (type(exc).__name__, exc)
            fail(
                "outliner_write_failed",
                "renaming %r to %r changed data-block references, which must never happen; the previous "
                "name was restored where possible" % (current, requested),
                details={"object": current, "requested_name": requested, "restored_name": restored,
                         "restore_error": restore_error, "before": before, "after": after},
            )
        return empty_result(
            name_of(target), object=requested, previous_name=current, name=requested,
            changed=True, idempotent=False, auto_suffix=False,
            references=after, references_preserved=True, membership=state,
            warnings=[
                "only the Object name changed: its data block, modifiers, Geometry Nodes groups, "
                "drivers, shape keys and collection membership are untouched",
            ],
        )

    # ================================================================= #
    # 7. outliner_object_delete
    # ================================================================= #
    @api.command("outliner_object_delete",
                 summary="delete exact objects of one scene and never their data blocks")
    def object_delete(scene, objects, confirm_delete):
        boolean(confirm_delete, "confirm_delete")
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        names = object_names(objects)
        resolved = [(name, object_named(name)) for name in names]
        scene_name = name_of(target)
        if not confirm_delete:
            fail(
                "confirmation_required",
                "deleting %d object datablock(s) (%s) is permanent and requires confirm_delete=true; "
                "nothing was changed" % (len(resolved), ", ".join(names[:MAX_NAME_SAMPLE])),
                details={"scene": scene_name, "objects": names, "confirm_delete": False,
                         "objects_deleted": [], "data_blocks_deleted": []},
            )

        # ---- whole-batch prevalidation (no writes) ----
        mapping = user_map_for([obj for _, obj in resolved])
        problems, plans = [], []
        for name, obj in resolved:
            state = membership(obj, index)
            state["scenes"] = sorted({scene_name for owner in owners_of(obj, index)
                                      for scene_name in index["in_scene"].get(key(owner), [])})
            if state["orphan"] or scene_name not in state["scenes"]:
                problems.append({
                    "code": "object_not_in_scene", "object": name, "reason": "outside_scene",
                    "scenes": state["scenes"],
                    "message": "object %r is not linked into scene %r (it belongs to %s)"
                               % (name, scene_name, ", ".join(state["scenes"]) or "no scene"),
                })
            elif len(state["scenes"]) > 1:
                problems.append({
                    "code": "object_shared_across_scenes", "object": name, "reason": "cross_scene",
                    "scenes": state["scenes"],
                    "message": "object %r belongs to scenes %s; deleting it here would delete it there too"
                               % (name, ", ".join(state["scenes"])),
                })
            if state["linked_object"] or state["override_object"]:
                problems.append({
                    "code": "read_only_object", "object": name, "reason": "linked_or_override",
                    "message": "object %r is linked from a library or carries a library override; it is "
                               "never deleted" % name,
                })
            in_use = object_external_users(obj, index, mapping)
            if in_use is not None:
                problems.append({
                    "code": "object_in_use", "object": name, "reason": in_use["reason"],
                    "users": in_use["users"], "user_count": in_use["user_count"],
                    "owners": in_use["owners"], "external_user_count": in_use["external_user_count"],
                    "message": "object %r is still referenced (reason %s by %s); a referenced object is "
                               "never deleted, so delete or re-point those users first"
                               % (name, in_use["reason"],
                                  ", ".join(entry["user"] or "<unknown>" for entry in in_use["users"])),
                })
            data = getattr(obj, "data", None)
            plans.append({
                "object": name,
                "key": key(obj),
                "type": getattr(obj, "type", None),
                "data": name_of(data) if data is not None else None,
                "data_id": data,
                "references": reference_snapshot(obj),
                "ownership": state,
            })
        if problems:
            first = problems[0]
            fail(
                first["code"],
                first["message"],
                details={"scene": scene_name, "objects": names, "problems": problems,
                         "problem_count": len(problems), "confirm_delete": True,
                         "objects_deleted": [], "data_blocks_deleted": []},
            )

        # ---- removal (permanent; nothing can be compensated) ----
        deleted, residual, errors = [], [], []
        for plan, (_, obj) in zip(plans, resolved):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
                deleted.append(plan["object"])
            except Exception as exc:  # noqa: BLE001 - the name is cached, the RNA is never read
                residual.append(plan["object"])
                errors.append({"object": plan["object"],
                               "error": "%s: %s" % (type(exc).__name__, exc)})
        # Data blocks are only *read* here (the removed Object is never touched again):
        # a surviving mesh/proxy/material group is exactly what must be preserved.
        data_preserved = []
        for plan in plans:
            data = plan["data_id"]
            if data is None:
                data_preserved.append({"object": plan["object"], "data": None,
                                       "data_type": None, "preserved": True})
                continue
            try:
                still_there = name_of(data)
                data_preserved.append({"object": plan["object"], "data": still_there,
                                       "data_type": getattr(data, "type", None), "preserved": True})
            except Exception:  # noqa: BLE001 - a freed data block would be a real defect
                data_preserved.append({"object": plan["object"], "data": plan["data"],
                                       "data_type": None, "preserved": False})
        freed_data = [entry["data"] for entry in data_preserved if not entry["preserved"]]
        if freed_data:
            fail("outliner_delete_partial", "object deletion unexpectedly released associated data; see applied changes",
                 details={"scene": scene_name, "objects_deleted": deleted,
                          "data_blocks_deleted": freed_data, "data_preserved": data_preserved,
                          "residual": residual, "partial": True, "not_transactional": True,
                          "rolled_back": False})
        if residual:
            fail(
                "outliner_delete_partial",
                "the object(s) %s were already removed and cannot be restored by this server; the "
                "deletion stopped at %s. This command is not transactional - the applied removals remain "
                "and are reported instead of being described as a rollback."
                % (", ".join(deleted) or "none", ", ".join(residual)),
                details={"scene": scene_name, "deleted": deleted, "residual": residual,
                         "remaining": residual, "failed": residual[0], "errors": errors,
                         "partial": True, "not_transactional": True, "rolled_back": False,
                         "objects_deleted": deleted, "data_blocks_deleted": freed_data,
                         "data_preserved": data_preserved},
            )
        warnings = [
            "object removal is permanent in Blender: this command is not transactional and never deletes "
            "mesh/material/Geometry Nodes data - orphan data is not purged",
        ]
        if freed_data:
            warnings.append(
                "unexpected: the data block(s) %s did not survive the object removal" % ", ".join(freed_data)
            )
        return {
            "scene": scene_name,
            "objects": names,
            "deleted": deleted,
            "objects_deleted": deleted,
            "object_count": len(deleted),
            "residual": [],
            "partial": False,
            "not_transactional": True,
            "rolled_back": False,
            "data_blocks_deleted": [],
            "data_preserved": data_preserved,
            "references": {plan["object"]: plan["references"] for plan in plans},
            "ownership": {plan["object"]: plan["ownership"] for plan in plans},
            "changed": True,
            "idempotent": False,
            "warnings": warnings,
            "limits": LIMITS,
        }

    # ================================================================= #
    # 8. outliner_collection_display
    # ================================================================= #
    @api.command("outliner_collection_display",
                 summary="set the eye, checkbox, monitor and render switches of one collection")
    def collection_display(scene, path, view_layer, hidden=None, disable_viewport=None,
                           hide_render=None, exclude=None):
        for value, label in ((hidden, "hidden"), (disable_viewport, "disable_viewport"),
                             (hide_render, "hide_render"), (exclude, "exclude")):
            if value is not None:
                boolean(value, label)
        if hidden is None and disable_viewport is None and hide_render is None and exclude is None:
            fail(
                "invalid_argument",
                "provide at least one of hidden, disable_viewport, hide_render or exclude; calling with "
                "everything null would change nothing",
                details={"supported": ["hidden", "disable_viewport", "hide_render", "exclude"]},
            )
        values = {"hidden": hidden, "disable_viewport": disable_viewport,
                  "hide_render": hide_render, "exclude": exclude}
        requested = {label: value for label, value in values.items() if value is not None}
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        collection, resolved = resolve(target, path, allow_root=True)
        layer = view_layer_named(target, view_layer)
        segments = parse_path(path, allow_root=True)
        layer_node = layer_collection_at(layer, segments, path, collection)

        datablock_labels = [label for label in requested if label in COLLECTION_DATABLOCK_FLAGS]
        if datablock_labels:
            # The monitor (Collection.hide_viewport) and the render flag are datablock-wide:
            # they must go through the shared single-owner guard, exactly like every other
            # write on the collection datablock.
            guard(target, collection, index, "collection %r" % name_of(collection))
        elif linked(collection):
            fail(
                "read_only_collection",
                "collection %r is linked from a library or carries a library override; it is never written"
                % name_of(collection),
                details={"collection": name_of(collection), "library": bool(getattr(collection, "library", None)),
                         "override": overridden(collection)},
            )

        targets = {}
        for label in COLLECTION_VIEW_LAYER_FLAGS:
            if label in requested:
                attribute, qualified = COLLECTION_VIEW_LAYER_FLAGS[label]
                targets[label] = (layer_node, attribute, qualified, "view_layer")
        for label in COLLECTION_DATABLOCK_FLAGS:
            if label in requested:
                attribute, qualified = COLLECTION_DATABLOCK_FLAGS[label]
                targets[label] = (collection, attribute, qualified, "datablock")
        for label, (block, attribute, qualified, _) in targets.items():
            if not hasattr(block, attribute):
                fail(
                    "api_unsupported",
                    "this Blender build does not expose %s" % qualified,
                    details={"collection": name_of(collection), "view_layer": name_of(layer),
                             "property": qualified, "switch": label},
                )
        before = {label: bool(getattr(block, attribute))
                  for label, (block, attribute, _, _) in targets.items()}
        pending = [label for label in requested if before[label] != requested[label]]
        if pending:
            run_batch("outliner_collection_display", [{
                "label": "set %s" % label,
                "apply": (lambda label=label: setattr(targets[label][0], targets[label][1], requested[label])),
                "undo": (lambda label=label: setattr(targets[label][0], targets[label][1], before[label])),
            } for label in pending])
        after = {label: bool(getattr(block, attribute))
                 for label, (block, attribute, _, _) in targets.items()}
        wrong = sorted(label for label in requested if after[label] != requested[label])
        if wrong:
            restored = {}
            for label in wrong:
                try:
                    setattr(targets[label][0], targets[label][1], before[label])
                    restored[label] = before[label]
                except error as exc:
                    restored[label] = "restore failed: %s" % exc.message
            fail(
                "outliner_write_failed",
                "Blender did not store the requested value for %s (read back %s)"
                % (", ".join(wrong), {label: after[label] for label in wrong}),
                details={"collection": name_of(collection), "view_layer": name_of(layer),
                         "requested": requested, "before": before, "after": after, "restored": restored},
            )
        changed = bool(pending)
        warnings = []
        if datablock_labels:
            warnings.append(
                "%s are datablock-wide switches: they apply to every view layer of scene %r, unlike the "
                "per-view-layer eye (hidden) and checkbox (exclude) which were written only in view layer %r"
                % (", ".join(sorted(datablock_labels)), name_of(target), name_of(layer))
            )
        if "hidden" not in requested and "exclude" not in requested:
            warnings.append(
                "no per-view-layer switch was requested: view layer %r was used only to address the "
                "outliner row, its eye and checkbox are unchanged" % name_of(layer)
            )
        return empty_result(
            name_of(target),
            view_layer=name_of(layer),
            path=resolved,
            collection=row(collection, resolved, index),
            requested=requested,
            before=before,
            after=after,
            changed=changed,
            idempotent=not changed,
            view_layer_flags=sorted(COLLECTION_VIEW_LAYER_FLAGS),
            datablock_flags=sorted(COLLECTION_DATABLOCK_FLAGS),
            monitor_supported=True,
            monitor_property="Collection.hide_viewport",
            warnings=warnings,
        )

    # ================================================================= #
    # 9. outliner_object_display
    # ================================================================= #
    @api.command("outliner_object_display",
                 summary="set the eye, monitor and render switches of exact ordinary objects")
    def object_display(scene, objects, view_layer, hidden=None, disable_viewport=None, hide_render=None):
        for value, label in ((hidden, "hidden"), (disable_viewport, "disable_viewport"),
                             (hide_render, "hide_render")):
            if value is not None:
                boolean(value, label)
        if hidden is None and disable_viewport is None and hide_render is None:
            fail(
                "invalid_argument",
                "provide hidden, disable_viewport and/or hide_render; calling with everything null would "
                "change nothing",
                details={"supported": ["hidden", "disable_viewport", "hide_render"]},
            )
        values = {"hidden": hidden, "disable_viewport": disable_viewport, "hide_render": hide_render}
        requested = {label: value for label, value in values.items() if value is not None}
        target = scene_named(scene, write=True)
        index = index_for(target, require_complete=True)
        layer = view_layer_named(target, view_layer)
        names = object_names(objects)
        resolved = [(name, object_named(name)) for name in names]

        # Whole batch first: every object must be writable in exactly this scene, so a
        # rejected batch writes nothing at all.
        states = {}
        for name, obj in resolved:
            scene_object(obj, target, index, "object %r" % name)
            for label in OBJECT_VIEW_LAYER_FLAGS:
                if label not in requested:
                    continue
                if not callable(getattr(obj, "hide_set", None)):
                    fail(
                        "api_unsupported",
                        "this Blender build does not expose Object.hide_set, so the per-view-layer hide "
                        "state cannot be written",
                        details={"object": name, "switch": label},
                    )
                if not callable(getattr(obj, "hide_get", None)):
                    fail(
                        "api_unsupported",
                        "this Blender build does not expose Object.hide_get, so the per-view-layer hide "
                        "state cannot be verified",
                        details={"object": name, "switch": label},
                    )
            for label in OBJECT_DATABLOCK_FLAGS:
                if label in requested and not hasattr(obj, OBJECT_DATABLOCK_FLAGS[label][0]):
                    fail(
                        "api_unsupported",
                        "this Blender build does not expose %s"
                        % OBJECT_DATABLOCK_FLAGS[label][1],
                        details={"object": name, "switch": label,
                                 "property": OBJECT_DATABLOCK_FLAGS[label][1]},
                    )
            states[name] = {
                "hidden": read_hidden(obj, layer) if "hidden" in requested else None,
                "disable_viewport": bool(getattr(obj, "hide_viewport", False)),
                "hide_render": bool(getattr(obj, "hide_render", False)),
            }
        steps = []
        for name, obj in resolved:
            if "hidden" in requested and states[name]["hidden"] != requested["hidden"]:
                steps.append({
                    "label": "hidden on %s in view layer %s" % (name, name_of(layer)),
                    "apply": (lambda obj=obj: write_hidden(obj, layer, requested["hidden"])),
                    "undo": (lambda obj=obj, old=states[name]["hidden"]: write_hidden(obj, layer, old)),
                })
            for label, (attribute, _) in OBJECT_DATABLOCK_FLAGS.items():
                if label in requested and states[name][label] != requested[label]:
                    steps.append({
                        "label": "%s on %s" % (label, name),
                        "apply": (lambda obj=obj, attribute=attribute, value=requested[label]:
                                  setattr(obj, attribute, value)),
                        "undo": (lambda obj=obj, attribute=attribute, old=states[name][label]:
                                 setattr(obj, attribute, old)),
                    })
        if steps:
            run_batch("outliner_object_display", steps)
        after, wrong = {}, []
        for name, obj in resolved:
            entry = {
                "hidden": read_hidden(obj, layer) if "hidden" in requested else None,
                "disable_viewport": bool(getattr(obj, "hide_viewport", False)),
                "hide_render": bool(getattr(obj, "hide_render", False)),
            }
            after[name] = entry
            for label in requested:
                if entry[label] != requested[label]:
                    wrong.append("%s.%s" % (name, label))
        if wrong:
            restored = {}
            for name, obj in resolved:
                if "hidden" in requested and after[name]["hidden"] != requested["hidden"]:
                    try:
                        write_hidden(obj, layer, states[name]["hidden"])
                        restored["%s.hidden" % name] = states[name]["hidden"]
                    except error as exc:
                        restored["%s.hidden" % name] = "restore failed: %s" % exc.message
                for label, (attribute, _) in OBJECT_DATABLOCK_FLAGS.items():
                    if label in requested and after[name][label] != requested[label]:
                        try:
                            setattr(obj, attribute, states[name][label])
                            restored["%s.%s" % (name, label)] = states[name][label]
                        except error as exc:
                            restored["%s.%s" % (name, label)] = "restore failed: %s" % exc.message
            fail(
                "outliner_write_failed",
                "Blender did not store the requested value for %s (read back %s)"
                % (", ".join(sorted(wrong)), after),
                details={"scene": name_of(target), "view_layer": name_of(layer), "requested": requested,
                         "before": states, "after": after, "restored": restored},
            )
        datablock_labels = sorted(label for label in requested if label in OBJECT_DATABLOCK_FLAGS)
        warnings = []
        if datablock_labels:
            warnings.append(
                "%s are datablock-wide switches on the object: they apply to every view layer of scene %r, "
                "unlike the per-view-layer eye (hidden) which was written only in view layer %r"
                % (", ".join(datablock_labels), name_of(target), name_of(layer))
            )
        if "hidden" not in requested:
            warnings.append(
                "no per-view-layer switch was requested: view layer %r was used only to address the "
                "outliner rows, their eye state is unchanged" % name_of(layer)
            )
        return empty_result(
            name_of(target),
            view_layer=name_of(layer),
            objects=[name for name, _ in resolved],
            requested=requested,
            before=states,
            after=after,
            changed=bool(steps),
            idempotent=not steps,
            view_layer_flags=sorted(OBJECT_VIEW_LAYER_FLAGS),
            datablock_flags=sorted(OBJECT_DATABLOCK_FLAGS),
            monitor_supported=True,
            monitor_property="Object.hide_viewport",
            warnings=warnings,
        )

    return {
        "module": "outliner_management",
        "commands": list(COMMAND_NAMES),
        "helpers": sorted(shared),
        "object_types": list(SUPPORTED_OBJECT_TYPES),
    }
