"""Native Outliner regressions: a pure in-memory fake ``bpy``, no Blender involved.

Two contracts are covered:

* the add-on module ``blender/outliner.py`` - collection-DAG queries, strict
  parameters, the write guards and batch atomicity/compensation;
* the MCP module ``blender_mcp/tools/outliner.py`` - the six typed tool schemas
  (title / annotations / ``meta`` domain) and what they forward to the bridge.

Nothing here touches ``Project`` or a real ``.blend``: the fake models
``bpy.data`` (scenes, collections, objects, node groups), ``Collection.children`` /
``Collection.objects`` as link/unlink relations, ``Object.users_collection`` and
Blender's file-wide collection name uniqueness (including its ``.001`` rename),
and it can inject failures into any link step.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / "blender" / "outliner.py"
RUNTIME = ROOT / ".runtime"
MISSING = object()


class Failure(Exception):
    """The bridge ``CommandError`` shape (code / message / details)."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}


# --------------------------------------------------------------------------- #
# fake Blender data model
# --------------------------------------------------------------------------- #
class Relations:
    """``Collection.children`` / ``Collection.objects``: an ordered link relation."""

    def __init__(self, blend, owner, kind):
        self._blend = blend
        self._owner = owner
        self._kind = kind
        self._items = []

    @staticmethod
    def _label(item):
        """Name for an error message; a removed ID has no readable name."""
        try:
            return repr(getattr(item, "name", item))
        except ReferenceError:
            return "<removed datablock>"

    def link(self, item):
        if self._kind == "children" and self._is_ancestor(item):
            raise RuntimeError("cannot make a collection a child of its own descendant")
        if any(existing is item for existing in self._items):
            raise RuntimeError("%s is already linked" % self._label(item))
        hook = self._blend.link_hook
        if hook is not None and hook(self._owner, item, self._kind):
            raise RuntimeError("injected %s link failure for %s" % (self._kind, self._label(item)))
        self._items.append(item)

    def unlink(self, item):
        hook = self._blend.unlink_hook
        if hook is not None and hook(self._owner, item, self._kind):
            raise RuntimeError("injected %s unlink failure for %s" % (self._kind, self._label(item)))
        for index, existing in enumerate(self._items):
            if existing is item:
                self._items.pop(index)
                return
        raise RuntimeError("%s is not linked" % self._label(item))

    def force(self, item):
        """Bypass every check - models a pathological file written by other scripts."""
        self._items.append(item)

    def _is_ancestor(self, candidate):
        stack, seen = [self._owner], set()
        while stack:
            node = stack.pop()
            if node is candidate:
                return True
            if id(node) in seen:
                continue
            seen.add(id(node))
            stack.extend(list(node.children))
        return False

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class Collection:
    """A fake ``bpy.types.Collection``.

    A removed datablock invalidates its RNA: like Blender, reading ``name`` or
    calling ``as_pointer()`` afterwards raises ``ReferenceError`` instead of
    returning stale data.
    """

    def __init__(self, blend, name, library=None, override_library=None):
        self._blend = blend
        self._removed = False
        self._name = name
        self._children = Relations(blend, self, "children")
        self._objects = Relations(blend, self, "objects")
        self.library = library
        self.override_library = override_library
        self.is_library_indirect = False
        self.hide_viewport = False
        self.hide_render = False

    def _check_live(self):
        if self._removed:
            raise ReferenceError("StructRNA of type Collection has been removed")

    @property
    def name(self):
        self._check_live()
        return self._name

    @property
    def children(self):
        self._check_live()
        return self._children

    @property
    def objects(self):
        self._check_live()
        return self._objects

    def as_pointer(self):
        self._check_live()
        return id(self)

    @property
    def children_recursive(self):
        found, seen, stack = [], set(), list(self.children)
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            found.append(node)
            stack.extend(list(node.children))
        return found

    @property
    def all_objects(self):
        found, seen, stack = [], set(), [self]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            for obj in node.objects:
                if not any(existing is obj for existing in found):
                    found.append(obj)
            stack.extend(list(node.children))
        return found


class Object:
    def __init__(self, blend, name, type="MESH", node_group=None, library=None, override_library=None):
        self._blend = blend
        self.name = name
        self.type = type
        self.library = library
        self.override_library = override_library
        self.is_library_indirect = False
        self.data = types.SimpleNamespace(name=name + "_data")
        self.modifiers = [types.SimpleNamespace(type="NODES", node_group=node_group)]
        self.matrix_world = (1.0, 2.0, 3.0)
        self.hide_viewport = False
        self.hide_render = False

    def as_pointer(self):
        return id(self)

    @property
    def users_collection(self):
        return [collection for collection in self._blend.all_collections()
                if any(existing is self for existing in collection.objects)]

    def snapshot(self):
        """Everything an Outliner write must never change."""
        return {
            "data": self.data.name,
            "matrix_world": tuple(self.matrix_world),
            "modifiers": [(modifier.type, getattr(modifier.node_group, "name", None))
                          for modifier in self.modifiers],
            "hide_viewport": self.hide_viewport,
            "hide_render": self.hide_render,
        }


class CollectionsRegistry:
    """``bpy.data.collections`` with Blender's file-wide name uniqueness."""

    def __init__(self, blend):
        self._blend = blend
        self._items = []
        self.new_calls = 0
        self.remove_calls = 0

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __contains__(self, collection):
        return any(item is collection for item in self._items)

    def get(self, name, default=None):
        for item in self._items:
            if item.name == name:
                return item
        return default

    def names(self):
        return [item.name for item in self._items]

    def new(self, name):
        self.new_calls += 1
        final, suffix = name, 0
        while self._blend.name_taken(final):
            suffix += 1
            final = "%s.%03d" % (name, suffix)
        if self._blend.force_new_name is not None:
            final = self._blend.force_new_name
            self._blend.force_new_name = None
        collection = Collection(self._blend, final)
        self._items.append(collection)
        return collection

    def remove(self, collection, do_unlink=True, do_id_user=True, do_ui_user=True):
        """``BlendDataCollections.remove``: the datablock is freed, its RNA dies."""
        self.remove_calls += 1
        for index, item in enumerate(self._items):
            if item is collection:
                self._items.pop(index)
                break
        else:
            raise RuntimeError("%r is not a collection" % getattr(collection, "_name", collection))
        # Model do_unlink=True while the struct is still alive, then invalidate it:
        # from here on every attribute access raises ReferenceError, exactly like a
        # freed Blender datablock.
        for block in self._blend.all_collections():
            block.children._items = [child for child in block.children if child is not collection]
            block.objects._items = [obj for obj in block.objects if obj is not collection]
        collection._removed = True
        collection._children._items = []
        collection._objects._items = []
        hook = self._blend.remove_hook
        if hook is not None:
            # Models "the datablock is gone, but Blender reported an error anyway":
            # the caller must not read the removed ID to report the failure.
            hook(collection)


class Scene:
    def __init__(self, blend, name, master=None, library=None, override_library=None):
        self._blend = blend
        self.name = name
        self.collection = master if master is not None else Collection(blend, name)
        self.library = library
        self.override_library = override_library

    def as_pointer(self):
        return id(self)

    @property
    def objects(self):
        return self.collection.all_objects


class FakeData:
    """The whole fake ``bpy.data`` plus the fixture helpers used by the tests."""

    def __init__(self):
        self.scenes = []
        self.collections = CollectionsRegistry(self)
        self.objects = []
        self.node_groups = []
        self.link_hook = None
        self.unlink_hook = None
        self.remove_hook = None
        self.force_new_name = None

    # -- queries ------------------------------------------------------- #
    def all_collections(self):
        found, seen = [], set()
        candidates = [scene.collection for scene in self.scenes]
        candidates.extend(list(self.collections))
        for block in candidates:
            if id(block) in seen:
                continue
            seen.add(id(block))
            found.append(block)
        return found

    def name_taken(self, name):
        return any(getattr(block, "name", None) == name for block in self.all_collections())

    def object(self, name):
        for item in self.objects:
            if item.name == name:
                return item
        return None

    def collection(self, name):
        for item in self.all_collections():
            if item.name == name:
                return item
        return None

    # -- fixture builders ---------------------------------------------- #
    def add_scene(self, name, master_name=None):
        scene = Scene(self, name, Collection(self, master_name or name))
        self.scenes.append(scene)
        return scene

    def add_collection(self, name, parents=(), objects=(), library=None, override_library=None):
        collection = Collection(self, name, library=library, override_library=override_library)
        self.collections._items.append(collection)
        for parent in parents:
            parent.children.link(collection)
        for obj in objects:
            collection.objects.link(obj)
        return collection

    def add_object(self, name, collections=(), node_group=None, library=None, override_library=None):
        obj = Object(self, name, node_group=node_group, library=library,
                     override_library=override_library)
        self.objects.append(obj)
        for collection in collections:
            collection.objects.link(obj)
        return obj


# --------------------------------------------------------------------------- #
# module loading (the stub is only needed while ``blender/outliner.py`` imports)
# --------------------------------------------------------------------------- #
_STUB = types.ModuleType("bpy")
_STUB.data = FakeData()
_PREVIOUS_BPY = sys.modules.get("bpy", MISSING)
sys.modules["bpy"] = _STUB
try:
    _SPEC = importlib.util.spec_from_file_location("outliner_under_test", MODULE_PATH)
    MODULE = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(MODULE)
finally:
    if _PREVIOUS_BPY is MISSING:
        sys.modules.pop("bpy", None)
    else:  # pragma: no cover - only when a real bpy is present
        sys.modules["bpy"] = _PREVIOUS_BPY

COMMANDS: dict = {}


def _command(name, mutating=True, summary=""):
    def wrapped(func):
        COMMANDS[name] = func
        return func

    return wrapped


MODULE.register_commands(types.SimpleNamespace(CommandError=Failure, command=_command))


class OutlinerTest(unittest.TestCase):
    """Base fixture: one scene ``Main`` with the root collection plus helpers."""

    def setUp(self):
        self.blend = FakeData()
        _STUB.data = self.blend
        self.group_a = types.SimpleNamespace(name="GN_A")
        self.group_b = types.SimpleNamespace(name="GN_B")
        self.blend.node_groups.extend([self.group_a, self.group_b])
        self.scene = self.blend.add_scene("Main")
        self.root = self.scene.collection

    # -- call helpers -------------------------------------------------- #
    def call(self, command, **params):
        return COMMANDS["outliner_" + command](**params)

    def error(self, code, command, **params):
        with self.assertRaises(Failure) as caught:
            self.call(command, **params)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def commands(self):
        return sorted(COMMANDS)

    def tree(self, payload, path):
        for entry in payload["collections"]:
            if entry["path"] == path:
                return entry
        self.fail("no collection row for path %r in %s"
                  % (path, [entry["path"] for entry in payload["collections"]]))


# --------------------------------------------------------------------------- #
# parameters and lookups
# --------------------------------------------------------------------------- #
class ArgumentTests(OutlinerTest):
    def test_exactly_six_commands_are_registered(self):
        self.assertEqual(self.commands(), [
            "outliner_ensure_path", "outliner_hierarchy", "outliner_link_objects",
            "outliner_move_objects", "outliner_set_visibility", "outliner_unlink_objects",
        ])

    def test_strict_booleans(self):
        self.error("invalid_argument", "ensure_path", scene="Main", path="A", create_missing="yes")
        self.error("invalid_argument", "unlink_objects", scene="Main", path="", objects=["O"], confirm_orphan=1)
        self.error("invalid_argument", "hierarchy", scene="Main", include_memberships=None)
        self.error("invalid_argument", "set_visibility", scene="Main", path="", hide_viewport=1)
        self.error("invalid_argument", "set_visibility", scene="Main", path="", hide_viewport="true")
        self.error("invalid_argument", "set_visibility", scene="Main", path="")

    def test_strict_object_lists(self):
        self.error("invalid_objects", "link_objects", scene="Main", path="", objects=[])
        self.error("invalid_objects", "link_objects", scene="Main", path="", objects="Cube")
        self.error("invalid_objects", "link_objects", scene="Main", path="", objects=["  "])
        self.error("duplicate_object", "link_objects", scene="Main", path="", objects=["Cube", "Cube"])
        self.error("invalid_objects", "link_objects", scene="Main", path="", objects=[17])

    def test_path_grammar(self):
        for bad in ("/A", "A/", "A//B", "A\\B", "./A", "A/../B", "A/./B", "A/ B", "A" * 256):
            self.error("invalid_path", "ensure_path", scene="Main", path=bad)
        # The empty string only addresses an existing root.
        self.error("invalid_path", "ensure_path", scene="Main", path="")
        self.error("invalid_path", "ensure_path", scene="Main", path=None)
        self.error("invalid_path", "link_objects", scene="Main", path=7, objects=["O"])

    def test_scene_lookup_never_uses_the_active_scene(self):
        self.error("invalid_scene", "hierarchy", scene="")
        self.error("invalid_scene", "hierarchy", scene=" Main")
        self.error("scene_not_found", "hierarchy", scene="Ghost")
        self.blend.add_scene("Main")
        self.error("ambiguous_scene", "hierarchy", scene="Main")

    def test_object_lookup_rejects_ambiguity_and_unknowns(self):
        self.blend.add_object("Dup", collections=[self.root])
        self.blend.add_object("Dup", collections=[self.root], library=object())
        self.error("ambiguous_object", "link_objects", scene="Main", path="", objects=["Dup"])
        self.error("object_not_found", "link_objects", scene="Main", path="", objects=["Ghost"])

    def test_linked_and_overridden_collections_are_never_written(self):
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        override = self.blend.add_collection("Override", parents=[self.root], override_library=object())
        obj = self.blend.add_object("Cube", collections=[self.root])
        self.error("read_only_collection", "link_objects", scene="Main", path="Linked", objects=["Cube"])
        self.error("read_only_collection", "set_visibility", scene="Main", path="Override", hide_viewport=True)
        self.error("read_only_collection", "ensure_path", scene="Main", path="Linked/New")
        self.assertFalse(linked.hide_viewport)
        self.assertEqual(list(linked.objects), [])
        self.assertEqual(list(override.children), [])
        self.assertEqual(obj.users_collection, [self.root])

    def test_module_never_uses_context_or_operators(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for needle in ("bpy.context.", "bpy.ops.", "bpy.types.", "eval(", "exec("):
            self.assertNotIn(needle, source, "forbidden reference %r in blender/outliner.py" % needle)
        # The stub has no ``context``/``ops`` attribute at all, so any such access
        # would raise AttributeError instead of silently using a UI context.
        self.assertIsNone(getattr(_STUB, "context", None))
        self.assertIsNone(getattr(_STUB, "ops", None))


# --------------------------------------------------------------------------- #
# read: hierarchy
# --------------------------------------------------------------------------- #
class HierarchyTests(OutlinerTest):
    def test_dag_multi_parent_is_not_a_cycle(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        props = self.blend.add_collection("Props", parents=[self.root, assets])
        self.blend.add_object("Cube", collections=[props])
        payload = self.call("hierarchy", scene="Main")
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["cycles"], [])
        row = self.tree(payload, "Props")
        self.assertEqual(row["parent_count"], 2)
        self.assertTrue(row["multi_parent"])
        self.assertEqual(sorted(row["parents"]), ["Assets", "Main"])
        self.assertEqual(sorted(row["scene_paths"]), ["Assets/Props", "Props"])
        self.assertEqual([entry["path"] for entry in payload["collections"]], ["", "Assets", "Props", "Assets/Props"])
        self.assertEqual(payload["scene_master"]["name"], "Main")
        self.assertEqual(payload["distinct_collection_count"], 3)
        # Root row.
        self.assertTrue(payload["collections"][0]["root"])
        self.assertEqual(self.tree(payload, "")["name"], "Main")

    def test_root_scene_with_the_same_name_as_a_collection_segment(self):
        self.blend.add_scene("Shared")
        moved = self.blend.add_scene("Moved")
        payload = self.call("hierarchy", scene="Moved")
        self.assertEqual(payload["scene_master"]["name"], "Moved")
        # A collection named like the *other* scene's root already occupies that
        # name file-wide, so creating it here would be renamed to Shared.001.
        caught = self.error("collection_name_conflict", "ensure_path", scene="Moved", path="Shared")
        self.assertEqual(caught.details["existing"][0]["collection"], "Shared")
        self.assertFalse(caught.details["auto_suffix"])
        self.assertNotIn("Shared.001", self.blend.collections.names())
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(list(moved.collection.children), [])

    def test_memberships_report_global_ownership(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        cube = self.blend.add_object("Cube", collections=[self.root, assets],
                                     node_group=self.group_a)
        self.blend.add_object("Loose", collections=[])
        payload = self.call("hierarchy", scene="Main", include_memberships=True)
        rows = {entry["name"]: entry for entry in payload["objects"]}
        self.assertEqual(rows["Cube"]["membership"]["scenes"], ["Main"])
        self.assertEqual(sorted(entry["collection"] for entry in rows["Cube"]["membership"]["collections"]),
                         ["Assets", "Main"])
        self.assertEqual(sorted(path for entry in rows["Cube"]["membership"]["collections"]
                               for path in entry["scene_paths"]), ["", "Assets"])
        self.assertFalse(rows["Cube"]["membership"]["orphan"])
        self.assertFalse(rows["Cube"]["membership"]["linked_object"])
        self.assertEqual(payload["object_count"], 1)
        # An object outside every collection is reported separately, never as a
        # collection member.
        self.assertEqual(payload["collectionless_objects"], ["Loose"])
        self.assertEqual(payload["global_data"]["node_groups"]["count"], 2)
        self.assertFalse(payload["global_data"]["node_groups"]["collection_member"])
        compact = self.call("hierarchy", scene="Main", include_memberships=False)
        self.assertNotIn("objects", compact)
        self.assertNotIn("collectionless_objects", compact)
        self.assertIs(compact["include_memberships"], False)
        self.assertEqual(cube.snapshot()["modifiers"], [("NODES", "GN_A")])

    def test_collection_shared_by_two_scenes_is_flagged(self):
        other = self.blend.add_scene("Other")
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        self.blend.add_object("Cube", collections=[shared])
        payload = self.call("hierarchy", scene="Main")
        row = self.tree(payload, "Shared")
        self.assertEqual(row["scenes"], ["Main", "Other"])
        self.assertTrue(row["cross_scene"])
        self.assertEqual(row["parent_count"], 2)
        self.error("collection_shared_across_scenes", "link_objects", scene="Main", path="Shared", objects=["Cube"])
        self.error("collection_shared_across_scenes", "ensure_path", scene="Main", path="Shared/New")
        self.error("collection_shared_across_scenes", "set_visibility", scene="Main", path="Shared",
                   hide_render=True)
        self.assertEqual(list(shared.children), [])

    def test_multi_parent_target_refuses_membership_writes(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        props = self.blend.add_collection("Props", parents=[self.root, assets])
        self.blend.add_object("Cube", collections=[self.root])
        self.error("collection_multi_parent", "link_objects", scene="Main", path="Props", objects=["Cube"])
        self.error("collection_multi_parent", "unlink_objects", scene="Main", path="Props", objects=["Cube"])
        self.error("collection_multi_parent", "ensure_path", scene="Main", path="Props/Sub")
        self.assertEqual(list(props.objects), [])

    def test_real_cycle_terminates_and_is_reported(self):
        first = self.blend.add_collection("First", parents=[self.root])
        second = self.blend.add_collection("Second", parents=[first])
        second.children.force(first)  # pathological file: First -> Second -> First
        payload = self.call("hierarchy", scene="Main")
        self.assertTrue(payload["cycles"], payload)
        self.assertIn(payload["cycles"][0]["collection"], {"First", "Second"})
        self.assertEqual(len(payload["collections"]) >= 3, True)

    def test_shared_ancestor_refuses_descendant_writes(self):
        branch = self.blend.add_collection("Branch", parents=[self.root])
        shared = self.blend.add_collection("Shared", parents=[self.root, branch])
        leaf = self.blend.add_collection("Leaf", parents=[shared])
        obj = self.blend.add_object("Cube", collections=[self.root])
        self.error("collection_multi_parent", "link_objects", scene="Main", path="Shared/Leaf", objects=["Cube"])
        self.error("collection_multi_parent", "set_visibility", scene="Main", path="Shared/Leaf", hide_render=True)
        self.assertEqual(list(leaf.objects), [])
        self.assertFalse(leaf.hide_render)
        self.assertEqual(obj.users_collection, [self.root])

    def test_ambiguous_child_name_is_refused(self):
        parent = self.blend.add_collection("Parent", parents=[self.root])
        self.blend.add_collection("Twin", parents=[parent])
        twin = Collection(self.blend, "Twin")
        parent.children.force(twin)  # two distinct children with the same exact name
        self.blend.add_object("Cube", collections=[self.root])
        self.error("ambiguous_collection", "link_objects", scene="Main", path="Parent/Twin", objects=["Cube"])
        self.error("ambiguous_collection", "ensure_path", scene="Main", path="Parent/Twin/Deep")
        self.assertEqual(list(parent.children), [self.blend.collection("Twin"), twin])

    def test_deep_hierarchy_is_truncated_but_still_writable(self):
        parent = self.root
        for depth in range(MODULE.MAX_DEPTH + 8):
            parent = self.blend.add_collection("Level%02d" % depth, parents=[parent])
        payload = self.call("hierarchy", scene="Main")
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["paths_truncated"])
        self.assertFalse(payload["graph_truncated"])
        self.blend.add_object("Cube", collections=[self.root])
        deep = "/".join("Level%02d" % index for index in range(MODULE.MAX_DEPTH + 8))
        result = self.call("link_objects", scene="Main", path=deep, objects=["Cube"])
        self.assertEqual(result["linked"], ["Cube"])

    def test_missing_path_is_reported_with_context(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        self.blend.add_collection("Sub", parents=[assets])
        self.blend.add_object("Cube", collections=[self.root])
        caught = self.error("collection_not_found", "link_objects", scene="Main",
                            path="Assets/Ghost", objects=["Cube"])
        self.assertEqual(caught.details["parent"], "Assets")
        self.assertEqual(caught.details["available"], ["Sub"])
        self.assertEqual(caught.details["resolved"], "Assets")


# --------------------------------------------------------------------------- #
# write: ensure_path
# --------------------------------------------------------------------------- #
class EnsurePathTests(OutlinerTest):
    def test_create_then_reuse_is_idempotent_and_writes_nothing_extra(self):
        first = self.call("ensure_path", scene="Main", path="Assets/Props/Sub")
        self.assertEqual(first["created"], ["Assets", "Props", "Sub"])
        self.assertEqual(first["reused"], [])
        self.assertTrue(first["changed"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(first["writable"])
        self.assertTrue(first["prechecked"])
        self.assertEqual(first["collection"]["path"], "Assets/Props/Sub")
        self.assertEqual(self.blend.collections.names(), ["Assets", "Props", "Sub"])
        self.assertEqual(self.blend.collections.new_calls, 3)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        second = self.call("ensure_path", scene="Main", path="Assets/Props/Sub")
        self.assertEqual(second["created"], [])
        self.assertEqual(second["reused"], ["Assets", "Props", "Sub"])
        self.assertFalse(second["changed"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(self.blend.collections.names(), ["Assets", "Props", "Sub"])
        # Full idempotence: no new datablock and no removal on the repeat call.
        self.assertEqual(self.blend.collections.new_calls, 3)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertFalse(any(".001" in name for name in self.blend.collections.names()))

    def test_late_conflict_is_prechecked_with_zero_new_and_zero_remove(self):
        props = self.blend.add_collection("Props", parents=[self.root])
        caught = self.error("collection_name_conflict", "ensure_path", scene="Main", path="One/Two/Props")
        self.assertEqual(caught.details["segment"], "Props")
        self.assertEqual(caught.details["reason"], "exists_elsewhere")
        # The whole path is validated first: nothing was created, nothing removed.
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertEqual(self.blend.collections.names(), ["Props"])
        self.assertEqual(list(self.root.children), [props])

    def test_duplicated_name_inside_the_path_is_prechecked(self):
        caught = self.error("collection_name_conflict", "ensure_path", scene="Main", path="New/New")
        self.assertEqual(caught.details["reason"], "duplicated_inside_path")
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(list(self.root.children), [])

    def test_reused_segments_are_guarded_too(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        props = self.blend.add_collection("Props", parents=[self.root, assets])
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        other = self.blend.add_scene("Other")
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        for path, code in (("Props", "collection_multi_parent"),
                           ("Props/Deep", "collection_multi_parent"),
                           ("Linked", "read_only_collection"),
                           ("Linked/Deep", "read_only_collection"),
                           ("Shared", "collection_shared_across_scenes"),
                           ("Shared/Deep", "collection_shared_across_scenes")):
            self.error(code, "ensure_path", scene="Main", path=path)
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertEqual(self.blend.collections.names(), ["Assets", "Props", "Linked", "Shared"])
        self.assertEqual(list(props.children), [])
        self.assertEqual(list(linked.children), [])
        self.assertEqual(list(shared.children), [])

    def test_create_missing_false_never_creates(self):
        caught = self.error("collection_not_found", "ensure_path", scene="Main", path="Assets/Props",
                            create_missing=False)
        self.assertEqual(caught.details["available"], [])
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(list(self.root.children), [])
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.call("ensure_path", scene="Main", path="Assets")
        self.assertEqual(self.blend.collections.new_calls, 1)
        caught = self.error("collection_not_found", "ensure_path", scene="Main", path="Assets/Props",
                            create_missing=False)
        self.assertEqual(caught.details["parent"], "Assets")
        self.assertEqual(self.blend.collections.names(), ["Assets"])
        self.assertEqual(self.blend.collections.new_calls, 1)

    def test_existing_name_elsewhere_is_refused_without_auto_suffix(self):
        props = self.blend.add_collection("Props", parents=[self.root])
        caught = self.error("collection_name_conflict", "ensure_path", scene="Main", path="Assets/Props")
        self.assertEqual(caught.details["existing"][0]["collection"], "Props")
        self.assertEqual(caught.details["reason"], "exists_elsewhere")
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertEqual(self.blend.collections.names(), ["Props"])
        self.assertEqual(list(self.root.children), [props])

    def test_partial_creation_is_cleaned_up_again(self):
        def hook(owner, item, kind):
            return kind == "children" and getattr(item, "name", None) == "Third"

        self.blend.link_hook = hook
        caught = self.error("outliner_write_failed", "ensure_path", scene="Main", path="One/Two/Third")
        self.assertEqual(caught.details["created"], ["One", "Two"])
        self.assertEqual(caught.details["cleanup"]["removed"], ["Third", "Two", "One"])
        self.assertEqual(caught.details["cleanup"]["kept"], [])
        self.assertEqual(caught.details["cleanup"]["failed"], [])
        self.assertTrue(caught.details["cleanup"]["clean"])
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(self.blend.collections.remove_calls, 3)
        self.assertEqual(list(self.root.children), [])

    def test_cleanup_reads_names_before_the_rna_is_removed(self):
        """A removed collection has no readable RNA: cleanup must use cached names."""
        collection = self.blend.add_collection("Gone", parents=[self.root])
        self.blend.collections.remove(collection)
        with self.assertRaises(ReferenceError):
            collection.name  # noqa: B018 - the access itself is the assertion
        with self.assertRaises(ReferenceError):
            collection.as_pointer()
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(list(self.root.children), [])
        # And the module still reports the *removed* name in its cleanup payload.
        self.blend.force_new_name = "New.001"
        caught = self.error("invalid_name", "ensure_path", scene="Main", path="New")
        self.assertEqual(caught.details["cleanup"]["removed"], ["New.001"])
        self.assertTrue(caught.details["cleanup"]["clean"])

    def test_cleanup_reports_a_remove_error_without_touching_the_removed_id(self):
        def remove_hook(collection):
            raise RuntimeError("injected remove failure")

        def link_hook(owner, item, kind):
            return kind == "children" and getattr(item, "name", None) == "Two"

        self.blend.link_hook = link_hook
        self.blend.remove_hook = remove_hook
        caught = self.error("outliner_write_failed", "ensure_path", scene="Main", path="One/Two/Three")
        cleanup = caught.details["cleanup"]
        # "Two" was allocated but never linked, "One" was linked: both removals are
        # rejected by the injected hook and reported with their cached names instead
        # of raising ReferenceError on the freed datablock.
        self.assertEqual(caught.details["created"], ["One"])
        self.assertEqual([entry["collection"] for entry in cleanup["failed"]], ["Two", "One"])
        for entry in cleanup["failed"]:
            self.assertIn("injected remove failure", entry["error"])
        self.assertFalse(cleanup["clean"])
        self.assertEqual(cleanup["kept"], [])
        self.assertEqual(self.blend.collections.names(), [])

    def test_renamed_creation_is_refused_and_removed(self):
        self.blend.force_new_name = "New.001"
        caught = self.error("invalid_name", "ensure_path", scene="Main", path="New")
        self.assertEqual(caught.details["stored_name"], "New.001")
        self.assertEqual(caught.details["cleanup"]["removed"], ["New.001"])
        self.assertEqual(caught.details["cleanup"]["clean"], True)
        self.assertEqual(self.blend.collections.new_calls, 1)
        self.assertEqual(self.blend.collections.remove_calls, 1)
        self.assertEqual(self.blend.collections.names(), [])
        self.assertEqual(list(self.root.children), [])

    def test_ensure_path_touches_no_object_and_no_node_group(self):
        cube = self.blend.add_object("Cube", collections=[self.root], node_group=self.group_a)
        before = cube.snapshot()
        self.call("ensure_path", scene="Main", path="Assets/Props")
        self.assertEqual(cube.snapshot(), before)
        self.assertEqual(cube.users_collection, [self.root])
        self.assertEqual([group.name for group in self.blend.node_groups], ["GN_A", "GN_B"])


# --------------------------------------------------------------------------- #
# write: link / move / unlink
# --------------------------------------------------------------------------- #
class LinkTests(OutlinerTest):
    def setUp(self):
        super().setUp()
        self.props = self.blend.add_collection("Props", parents=[self.root])

    def test_existing_objects_and_collections_are_never_deleted(self):
        assets = self.blend.add_collection("Assets", parents=[self.root])
        cube = self.blend.add_object("Cube", collections=[self.root, assets], node_group=self.group_a)
        ghost = self.blend.add_object("Ghost", collections=[self.root])
        before = cube.snapshot()
        object_ids = [id(obj) for obj in self.blend.objects]
        collection_ids = sorted(id(coll) for coll in self.blend.all_collections())
        # A membership batch that fails on its last object must leave every
        # pre-existing datablock (and its ownership) untouched.
        self.blend.link_hook = lambda owner, item, kind: getattr(item, "name", None) == "Ghost"
        self.error("outliner_write_failed", "link_objects", scene="Main", path="Assets",
                   objects=["Cube", "Ghost"])
        self.assertEqual([id(obj) for obj in self.blend.objects], object_ids)
        self.assertEqual(sorted(id(coll) for coll in self.blend.all_collections()), collection_ids)
        self.assertIs(self.blend.object("Cube"), cube)
        self.assertIs(self.blend.object("Ghost"), ghost)
        self.assertEqual(cube.snapshot(), before)
        self.assertEqual([coll.name for coll in cube.users_collection], ["Main", "Assets"])
        self.assertEqual([coll.name for coll in ghost.users_collection], ["Main"])
        # No collection was created or removed by a membership write.
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.blend.link_hook = None
        self.call("unlink_objects", scene="Main", path="Assets", objects=["Cube"])
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertIs(self.blend.object("Cube"), cube)
        self.assertEqual(len(self.blend.objects), 2)

    def test_link_is_additive_idempotent_and_reports_ownership(self):
        cube = self.blend.add_object("Cube", collections=[self.root], node_group=self.group_a)
        sphere = self.blend.add_object("Sphere", collections=[self.root])
        first = self.call("link_objects", scene="Main", path="Props", objects=["Cube", "Sphere"])
        self.assertEqual(first["linked"], ["Cube", "Sphere"])
        self.assertEqual(first["skipped"], [])
        self.assertEqual(first["unlinked"], [])
        self.assertEqual(first["objects_deleted"], [])
        self.assertEqual(sorted(entry["collection"] for entry in first["before"]["Cube"]["collections"]), ["Main"])
        self.assertEqual(sorted(entry["collection"] for entry in first["after"]["Cube"]["collections"]),
                         ["Main", "Props"])
        after_props = next(entry for entry in first["after"]["Cube"]["collections"]
                           if entry["collection"] == "Props")
        self.assertEqual(after_props["scene_paths"], ["Props"])
        self.assertEqual(after_props["scenes"], ["Main"])
        self.assertFalse(after_props["outside_scene"])
        # Root membership is untouched (never unlinked by a link).
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main", "Props"])
        self.assertEqual(cube.snapshot()["modifiers"], [("NODES", "GN_A")])
        repeat = self.call("link_objects", scene="Main", path="Props", objects=["Cube", "Sphere"])
        self.assertEqual(repeat["linked"], [])
        self.assertEqual(repeat["skipped"], ["Cube", "Sphere"])
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])

    def test_root_path_is_the_master_collection(self):
        cube = self.blend.add_object("Cube")
        result = self.call("link_objects", scene="Main", path="", objects=["Cube"])
        self.assertEqual(result["path"], "")
        self.assertEqual(result["collection"]["name"], "Main")
        self.assertTrue(result["collection"]["root"])
        self.assertEqual(cube.users_collection, [self.root])

    def test_whole_batch_is_validated_before_mutation(self):
        self.blend.add_object("Cube", collections=[])
        self.error("object_not_found", "link_objects", scene="Main", path="Props", objects=["Cube", "Ghost"])
        self.assertEqual(list(self.props.objects), [])
        self.assertEqual(list(self.root.objects), [])

    def test_injected_failure_is_compensated_and_reported(self):
        for name in ("A", "B", "C"):
            self.blend.add_object(name, collections=[self.root])
        self.blend.link_hook = lambda owner, item, kind: kind == "objects" and getattr(item, "name", None) == "C"
        caught = self.error("outliner_write_failed", "link_objects", scene="Main", path="Props",
                            objects=["A", "B", "C"])
        self.assertEqual(caught.details["applied"], ["link A", "link B"])
        self.assertEqual(caught.details["compensated"], ["link B", "link A"])
        self.assertEqual(caught.details["residual"], [])
        self.assertTrue(caught.details["rolled_back"])
        self.assertEqual(list(self.props.objects), [])
        for name in ("A", "B", "C"):
            self.assertEqual([collection.name for collection in self.blend.object(name).users_collection], ["Main"])


class MoveTests(OutlinerTest):
    def setUp(self):
        super().setUp()
        self.props = self.blend.add_collection("Props", parents=[self.root])
        self.assets = self.blend.add_collection("Assets", parents=[self.root])

    def test_move_unlinks_only_the_listed_sources(self):
        cube = self.blend.add_object("Cube", collections=[self.root, self.props])
        sphere = self.blend.add_object("Sphere", collections=[self.root])
        result = self.call("move_objects", scene="Main", path="Assets", objects=["Cube", "Sphere"],
                           unlink_from=["Props"])
        self.assertEqual(result["unlinked"], ["Cube@Props"])
        self.assertEqual(result["linked"], ["Cube", "Sphere"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main", "Assets"])
        self.assertEqual([collection.name for collection in sphere.users_collection], ["Main", "Assets"])
        self.assertEqual(result["unlink_from"], ["Props"])
        self.assertTrue(result["changed"])

    def test_move_from_the_root_with_the_empty_string(self):
        cube = self.blend.add_object("Cube", collections=[self.root])
        result = self.call("move_objects", scene="Main", path="Props", objects=["Cube"], unlink_from=[""])
        self.assertEqual(result["unlinked"], ["Cube@<root>"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Props"])
        self.assertEqual(result["after"]["Cube"]["scenes"], ["Main"])

    def test_unlisted_memberships_are_never_touched(self):
        cube = self.blend.add_object("Cube", collections=[self.root, self.props, self.assets])
        self.call("move_objects", scene="Main", path="Props", objects=["Cube"], unlink_from=["Assets"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main", "Props"])

    def test_conflicting_and_duplicate_sources_are_refused_before_any_change(self):
        cube = self.blend.add_object("Cube", collections=[self.root])
        caught = self.error("conflicting_paths", "move_objects", scene="Main", path="Props",
                            objects=["Cube"], unlink_from=["Props"])
        self.assertEqual(caught.details["unlink_from"], "Props")
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main"])
        self.error("duplicate_path", "move_objects", scene="Main", path="Assets", objects=["Cube"],
                   unlink_from=["Props", "Props"])
        self.error("invalid_argument", "move_objects", scene="Main", path="Assets", objects=["Cube"],
                   unlink_from="Props")
        self.error("collection_not_found", "move_objects", scene="Main", path="Assets", objects=["Cube"],
                   unlink_from=["Ghost"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main"])

    def test_plan_is_precomputed_and_never_orphans_an_object(self):
        cube = self.blend.add_object("Cube", collections=[self.props])
        result = self.call("move_objects", scene="Main", path="Assets", objects=["Cube"], unlink_from=["Props"])
        self.assertEqual(result["planned"]["Cube"]["scenes"], ["Main"])
        self.assertEqual(result["planned"]["Cube"]["collections"], ["Assets"])
        self.assertFalse(result["planned"]["Cube"]["orphan"])
        self.assertFalse(result["objects_deleted"])

    def test_repeat_move_is_idempotent(self):
        cube = self.blend.add_object("Cube", collections=[self.props])
        self.call("move_objects", scene="Main", path="Assets", objects=["Cube"], unlink_from=["Props"])
        repeat = self.call("move_objects", scene="Main", path="Assets", objects=["Cube"], unlink_from=["Props"])
        self.assertEqual(repeat["linked"], [])
        self.assertEqual(repeat["unlinked"], [])
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Assets"])


class UnlinkTests(OutlinerTest):
    def setUp(self):
        super().setUp()
        self.props = self.blend.add_collection("Props", parents=[self.root])

    def test_unlink_breaks_only_the_relationship_and_keeps_data(self):
        cube = self.blend.add_object("Cube", collections=[self.root, self.props], node_group=self.group_a)
        before = cube.snapshot()
        result = self.call("unlink_objects", scene="Main", path="Props", objects=["Cube"])
        self.assertEqual(result["unlinked"], ["Cube"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["objects_deleted"], [])
        self.assertEqual(result["collections_deleted"], [])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Main"])
        self.assertIs(self.blend.object("Cube"), cube)
        self.assertEqual(len(self.blend.objects), 1)
        self.assertEqual(cube.snapshot(), before)
        self.assertEqual(result["before"]["Cube"]["collection_count"], 2)
        self.assertEqual(result["after"]["Cube"]["collection_count"], 1)

    def test_objects_that_are_not_linked_are_skipped(self):
        self.blend.add_object("Cube", collections=[self.root])
        result = self.call("unlink_objects", scene="Main", path="Props", objects=["Cube"])
        self.assertEqual(result["unlinked"], [])
        self.assertEqual(result["skipped"], ["Cube"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["idempotent"])

    def test_last_membership_needs_explicit_confirmation(self):
        cube = self.blend.add_object("Cube", collections=[self.props])
        caught = self.error("orphan_object", "unlink_objects", scene="Main", path="Props", objects=["Cube"])
        self.assertEqual(caught.details["objects"], ["Cube"])
        self.assertFalse(caught.details["object_deleted"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Props"])
        confirmed = self.call("unlink_objects", scene="Main", path="Props", objects=["Cube"],
                              confirm_orphan=True)
        self.assertEqual(confirmed["orphan_objects"], ["Cube"])
        self.assertTrue(confirmed["warnings"])
        self.assertEqual(confirmed["objects_deleted"], [])
        self.assertIs(self.blend.object("Cube"), cube)
        self.assertEqual(list(cube.users_collection), [])
        self.assertTrue(confirmed["after"]["Cube"]["orphan"])

    def test_collection_outside_every_scene_still_counts_as_detached(self):
        loose = self.blend.add_collection("Loose")
        cube = self.blend.add_object("Cube", collections=[self.props, loose])
        self.error("orphan_object", "unlink_objects", scene="Main", path="Props", objects=["Cube"])
        self.assertEqual(sorted(collection.name for collection in cube.users_collection), ["Loose", "Props"])
        confirmed = self.call("unlink_objects", scene="Main", path="Props", objects=["Cube"],
                              confirm_orphan=True)
        self.assertEqual(confirmed["orphan_objects"], ["Cube"])
        self.assertEqual([collection.name for collection in cube.users_collection], ["Loose"])
        self.assertEqual(confirmed["after"]["Cube"]["scenes"], [])
        self.assertFalse(confirmed["after"]["Cube"]["orphan"])

    def test_batch_orphan_check_covers_the_whole_batch(self):
        first = self.blend.add_object("First", collections=[self.props])
        second = self.blend.add_object("Second", collections=[self.props, self.root])
        caught = self.error("orphan_object", "unlink_objects", scene="Main", path="Props",
                            objects=["First", "Second"])
        self.assertEqual(caught.details["objects"], ["First"])
        self.assertEqual([collection.name for collection in first.users_collection], ["Props"])
        self.assertEqual(sorted(collection.name for collection in second.users_collection), ["Main", "Props"])

    def test_injected_unlink_failure_relinks_and_reports(self):
        for name in ("A", "B"):
            self.blend.add_object(name, collections=[self.props, self.root])
        self.blend.unlink_hook = lambda owner, item, kind: kind == "objects" and getattr(item, "name", None) == "B"
        caught = self.error("outliner_write_failed", "unlink_objects", scene="Main", path="Props",
                            objects=["A", "B"])
        self.assertEqual(caught.details["applied"], ["unlink A"])
        self.assertEqual(caught.details["compensated"], ["unlink A"])
        self.assertTrue(caught.details["rolled_back"])
        self.assertEqual(sorted(collection.name for collection in self.blend.object("A").users_collection),
                         ["Main", "Props"])


# --------------------------------------------------------------------------- #
# write: set_visibility
# --------------------------------------------------------------------------- #
class VisibilityTests(OutlinerTest):
    def setUp(self):
        super().setUp()
        self.props = self.blend.add_collection("Props", parents=[self.root])

    def test_visibility_is_written_read_back_and_idempotent(self):
        cube = self.blend.add_object("Cube", collections=[self.props])
        result = self.call("set_visibility", scene="Main", path="Props",
                           hide_viewport=True, hide_render=True)
        self.assertEqual(result["after"], {"hide_viewport": True, "hide_render": True})
        self.assertEqual(result["before"], {"hide_viewport": False, "hide_render": False})
        self.assertTrue(result["changed"])
        self.assertFalse(result["idempotent"])
        self.assertTrue(self.props.hide_viewport and self.props.hide_render)
        # Object flags are a different property and are never touched.
        self.assertFalse(cube.hide_viewport or cube.hide_render)
        repeat = self.call("set_visibility", scene="Main", path="Props",
                           hide_viewport=True, hide_render=True)
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])

    def test_partial_update_preserves_the_other_flag(self):
        self.props.hide_render = True
        result = self.call("set_visibility", scene="Main", path="Props", hide_viewport=True)
        self.assertEqual(result["requested"], {"hide_viewport": True})
        self.assertEqual(result["after"], {"hide_viewport": True})
        self.assertTrue(self.props.hide_render)
        self.assertTrue(self.props.hide_viewport)

    def test_root_visibility_and_missing_property(self):
        result = self.call("set_visibility", scene="Main", path="", hide_render=True)
        self.assertEqual(result["path"], "")
        self.assertTrue(self.root.hide_render)
        self.error("collection_not_found", "set_visibility", scene="Main", path="Ghost", hide_render=True)
        del self.props.hide_render
        self.error("api_unsupported", "set_visibility", scene="Main", path="Props", hide_render=True)


# --------------------------------------------------------------------------- #
# MCP tool surface
# --------------------------------------------------------------------------- #
class StubBridge:
    def __init__(self):
        self.calls = []

    def call(self, command, params=None, timeout=None):
        self.calls.append({"command": command, "params": dict(params or {})})
        return {"ok": True, "command": command}

    def close(self):
        pass

    def commands(self):
        return [entry["command"] for entry in self.calls]

    def params_of(self, command):
        for entry in self.calls:
            if entry["command"] == command:
                return entry["params"]
        return {}


def _structured(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        data = structured.get("result", structured)
        return data if isinstance(data, dict) else {"value": data}
    content = result if isinstance(result, list) else getattr(result, "content", None)
    for item in content or []:
        text = getattr(item, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


class McpToolTests(unittest.TestCase):
    EXPECTED = {
        "blender_outliner_hierarchy": "outliner_hierarchy",
        "blender_outliner_ensure_path": "outliner_ensure_path",
        "blender_outliner_link_objects": "outliner_link_objects",
        "blender_outliner_move_objects": "outliner_move_objects",
        "blender_outliner_unlink_objects": "outliner_unlink_objects",
        "blender_outliner_set_visibility": "outliner_set_visibility",
    }

    @classmethod
    def setUpClass(cls):
        from blender_mcp import audit
        from blender_mcp.tools import AuditedFastMCP
        from blender_mcp.tools import outliner as tools

        # Test-local audit directory: nothing here touches the shared log area.
        audit.configure(directory=RUNTIME / "outliner_test_audit", source="test")
        cls.server = AuditedFastMCP(name="outliner-tests")
        cls.bridge = StubBridge()
        tools.register_outliner_tools(cls.server, cls.bridge)
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.server.list_tools())}

    def test_register_alias_is_the_same_callable(self):
        from blender_mcp.tools import outliner as tools

        self.assertIs(tools.register, tools.register_outliner_tools)

    def test_six_named_tools_with_typed_schemas(self):
        self.assertEqual(sorted(self.tools), sorted(self.EXPECTED))
        hierarchy = self.tools["blender_outliner_hierarchy"].inputSchema
        self.assertEqual(hierarchy["required"], ["scene"])
        self.assertEqual(hierarchy["properties"]["include_memberships"]["default"], True)
        ensure = self.tools["blender_outliner_ensure_path"].inputSchema
        self.assertEqual(ensure["required"], ["scene", "path"])
        self.assertEqual(ensure["properties"]["path"]["maxLength"], 1024)
        self.assertEqual(ensure["properties"]["create_missing"]["default"], True)
        self.assertEqual(ensure["properties"]["scene"]["maxLength"], 255)
        link = self.tools["blender_outliner_link_objects"].inputSchema
        self.assertEqual(link["required"], ["scene", "path", "objects"])
        self.assertEqual(link["properties"]["objects"]["minItems"], 1)
        self.assertEqual(link["properties"]["objects"]["maxItems"], 512)
        self.assertEqual(link["properties"]["objects"]["items"]["maxLength"], 255)
        move = self.tools["blender_outliner_move_objects"].inputSchema
        self.assertEqual(move["required"], ["scene", "path", "objects", "unlink_from"])
        self.assertEqual(move["properties"]["unlink_from"]["maxItems"], 64)
        unlink = self.tools["blender_outliner_unlink_objects"].inputSchema
        self.assertEqual(unlink["properties"]["confirm_orphan"]["default"], False)
        visibility = self.tools["blender_outliner_set_visibility"].inputSchema
        self.assertEqual(sorted(visibility["properties"]), ["hide_render", "hide_viewport", "path", "scene"])
        self.assertIsNone(visibility["properties"]["hide_viewport"]["default"])
        self.assertIsNone(visibility["properties"]["hide_render"]["default"])
        for name in self.EXPECTED:
            lowered = name.lower()
            for forbidden in ("eval", "exec", "python", "operator"):
                self.assertNotIn(forbidden, lowered)

    def test_title_annotations_and_domain_meta(self):
        expectations = {
            "blender_outliner_hierarchy": (True, False),
            "blender_outliner_ensure_path": (False, False),
            "blender_outliner_link_objects": (False, False),
            "blender_outliner_move_objects": (False, True),
            "blender_outliner_unlink_objects": (False, True),
            "blender_outliner_set_visibility": (False, False),
        }
        for name, (read_only, destructive) in expectations.items():
            tool = self.tools[name]
            self.assertTrue(tool.title)
            self.assertEqual(tool.annotations.readOnlyHint, read_only, name)
            self.assertEqual(tool.annotations.destructiveHint, destructive, name)
            self.assertEqual(tool.annotations.idempotentHint, True, name)
            self.assertEqual(tool.annotations.openWorldHint, False, name)
            self.assertEqual(tool.meta["domain"], "outliner", name)
            self.assertEqual(tool.meta["bridge_prefix"], "outliner_", name)
            self.assertIs(tool.meta["deletes_data"], False, name)
            self.assertEqual(tool.meta["mutating"], not read_only, name)

    def test_forwarding_is_typed_and_never_imports_bpy(self):
        self.assertNotIn("bpy", sys.modules)
        cases = (
            ("blender_outliner_hierarchy", {"scene": "Main", "include_memberships": False},
             "outliner_hierarchy", {"scene": "Main", "include_memberships": False}),
            ("blender_outliner_ensure_path", {"scene": "Main", "path": "A/B", "create_missing": False},
             "outliner_ensure_path", {"scene": "Main", "path": "A/B", "create_missing": False}),
            ("blender_outliner_link_objects", {"scene": "Main", "path": "", "objects": ["A", "B"]},
             "outliner_link_objects", {"scene": "Main", "path": "", "objects": ["A", "B"]}),
            ("blender_outliner_move_objects",
             {"scene": "Main", "path": "A", "objects": ["B"], "unlink_from": ["", "C"]},
             "outliner_move_objects", {"scene": "Main", "path": "A", "objects": ["B"], "unlink_from": ["", "C"]}),
            ("blender_outliner_unlink_objects",
             {"scene": "Main", "path": "A", "objects": ["B"], "confirm_orphan": True},
             "outliner_unlink_objects", {"scene": "Main", "path": "A", "objects": ["B"], "confirm_orphan": True}),
            ("blender_outliner_set_visibility",
             {"scene": "Main", "path": "A", "hide_viewport": True},
             "outliner_set_visibility",
             {"scene": "Main", "path": "A", "hide_viewport": True, "hide_render": None}),
        )
        for tool_name, arguments, command, expected in cases:
            payload = _structured(asyncio.run(self.server.call_tool(tool_name, arguments)))
            self.assertIs(payload.get("ok"), True, tool_name)
            self.assertEqual(self.bridge.commands()[-1], command, tool_name)
            self.assertEqual(self.bridge.params_of(command), expected, tool_name)

    def test_schema_rejects_wrong_types_before_the_bridge(self):
        for tool_name, arguments in (
            ("blender_outliner_hierarchy", {"scene": ""}),
            ("blender_outliner_hierarchy", {"scene": "Main", "include_memberships": "maybe"}),
            ("blender_outliner_ensure_path", {"scene": "Main", "path": "A" * 1025}),
            ("blender_outliner_link_objects", {"scene": "Main", "path": "", "objects": []}),
            ("blender_outliner_link_objects", {"scene": "Main", "path": "", "objects": "A"}),
            ("blender_outliner_move_objects", {"scene": "Main", "path": "A", "objects": ["B"]}),
            ("blender_outliner_set_visibility", {"scene": "Main", "path": "A", "hide_viewport": "yes"}),
            ("blender_outliner_set_visibility", {"scene": "Main", "path": "A", "hide_render": 2}),
        ):
            before = len(self.bridge.calls)
            with self.assertRaises(Exception, msg="%s %s" % (tool_name, arguments)):
                asyncio.run(self.server.call_tool(tool_name, arguments))
            self.assertEqual(len(self.bridge.calls), before, tool_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
