"""Native Outliner *management* regressions: a pure in-memory fake ``bpy``, no Blender.

Contracts covered:

* the add-on module ``blender/outliner_management.py`` - collection lifecycle
  (create/rename/move/delete), ordinary Mesh/Empty object
  create/rename/delete and the Outliner display switches, with the same whole-batch
  prevalidation/compensation and no-suffix naming rules as the existing Outliner package;
* the helper contract: the module reuses the primitives published by
  ``blender/outliner.py:register_commands(api)`` (which now returns its helper dict,
  so no source rewriting is needed anywhere);
* the MCP module ``blender_mcp/tools/outliner_management.py`` - the nine typed tool
  schemas (title / annotations / ``meta`` domain), their forwarding to the bridge, the
  audit decorator around them and registration through the domain proxy.

The fake model of ``tests/test_outliner.py`` is imported and *extended* (never
modified): settable collection/object names with Blender's ``.001`` collision
behaviour, real ``bpy.data.objects`` / ``bpy.data.meshes`` registries with
``new``/``remove``/RNA invalidation, ``Scene.view_layers`` / ``LayerCollection`` with
persistent per-layer flags, ``Object.hide_set``/``hide_get``, object references
(parent/constraint/modifier/node tree), ``bpy.data.user_map``, and injectable
failures / ignored writes.

Nothing here connects to Blender, opens a ``.blend`` or writes into ``Project``.
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

RUNTIME = ROOT / ".runtime"
OUTLINER_PATH = ROOT / "blender" / "outliner.py"
MANAGEMENT_PATH = ROOT / "blender" / "outliner_management.py"
TOOLS_PATH = ROOT / "blender_mcp" / "tools" / "outliner_management.py"
MISSING = object()

# --------------------------------------------------------------------------- #
# the existing in-memory model, reused and extended
# --------------------------------------------------------------------------- #
def _load_base_model():
    try:
        from tests import test_outliner as base  # noqa: PLC0415 - preferred import
        return base
    except ImportError:  # pragma: no cover - when the package layout differs
        spec = importlib.util.spec_from_file_location("outliner_base_model",
                                                     ROOT / "tests" / "test_outliner.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


BASE = _load_base_model()
Failure = BASE.Failure
BaseCollection = BASE.Collection
BaseObject = BASE.Object
BaseScene = BASE.Scene
StubBridge = BASE.StubBridge
_structured = BASE._structured

#: Helper keys the management package reuses from ``blender/outliner.py``.
HELPER_KEYS = (
    "fail", "key", "name_of", "linked", "scene_named", "parse_path", "boolean",
    "object_names", "object_named", "all_collections", "index_for", "build_index",
    "resolve", "guard", "owners_of", "member_of", "membership", "row", "run_batch",
    "children_of", "objects_of", "children_rel", "objects_rel", "master_of",
    "write_target",
)

EXPECTED_COMMANDS = [
    "outliner_collection_create", "outliner_collection_delete", "outliner_collection_display",
    "outliner_collection_move", "outliner_collection_rename", "outliner_object_create",
    "outliner_object_delete", "outliner_object_display", "outliner_object_rename",
]

REMOVED_INSTANCE_COMMANDS = [
    "outliner_instance_create", "outliner_instance_info", "outliner_instance_rename",
    "outliner_instance_transform", "outliner_instance_delete", "outliner_instance_move",
]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# fake view layer / layer collection
# --------------------------------------------------------------------------- #
class LayerCollection:
    """A fake ``LayerCollection``: the per-view-layer eye and the checkbox.

    Flags live on this object (one per view layer + collection), like Blender's
    layer tree, so toggling one view layer never touches another.  A write listed in
    ``blend.ignored_writes`` is silently dropped, which models a build that refuses
    to store the value; the module must notice through its per-switch read-back.
    """

    def __init__(self, view_layer, collection):
        self._view_layer = view_layer
        self.collection = collection
        self._hide_viewport = False
        self._exclude = False

    @property
    def name(self):
        return self.collection.name

    @property
    def children(self):
        return [self._view_layer.layer_for(child) for child in self.collection.children]

    @property
    def hide_viewport(self):
        return self._hide_viewport

    @hide_viewport.setter
    def hide_viewport(self, value):
        if self._view_layer._blend.ignored_writes.get("LayerCollection.hide_viewport"):
            return
        self._hide_viewport = bool(value)

    @property
    def exclude(self):
        return self._exclude

    @exclude.setter
    def exclude(self, value):
        if self._view_layer._blend.ignored_writes.get("LayerCollection.exclude"):
            return
        self._exclude = bool(value)


class ViewLayer:
    """A fake ``ViewLayer``: ``layer_collection`` is built from the scene DAG."""

    def __init__(self, blend, scene, name):
        self._blend = blend
        self.scene = scene
        self.name = name
        self._layers = {}
        self.node_hook = None

    def as_pointer(self):
        return id(self)

    def layer_for(self, collection):
        key = id(collection)
        node = self._layers.get(key)
        if node is None:
            if self.node_hook is not None:
                node = self.node_hook(collection, self)
            else:
                node = LayerCollection(self, collection)
            self._layers[key] = node
        return node

    @property
    def layer_collection(self):
        return self.layer_for(self.scene.collection)


# --------------------------------------------------------------------------- #
# extended data model
# --------------------------------------------------------------------------- #
class LiveMesh:
    """A fake ``bpy.types.Mesh`` with Blender-like RNA invalidation."""

    def __init__(self, blend, name):
        self._blend = blend
        self._name = name
        self._removed = False
        self.vertices = []
        self.polygons = []
        self.materials = []
        self.shape_keys = None

    def _check_live(self):
        if self._removed:
            raise ReferenceError("StructRNA of type Mesh has been removed")

    @property
    def name(self):
        self._check_live()
        return self._name

    @name.setter
    def name(self, value):
        self._check_live()
        self._name = value

    def as_pointer(self):
        self._check_live()
        return id(self)

    @property
    def type(self):
        return "MESH"

    def from_pydata(self, vertices=(), edges=(), faces=()):
        """Models Blender: geometry is stored, out-of-range indices raise."""
        self._check_live()
        vertices = [list(vertex) for vertex in vertices]
        faces = [list(face) for face in faces]
        total = len(vertices)
        for position, face in enumerate(faces):
            for slot, index in enumerate(face):
                if not isinstance(index, int) or index < 0 or index >= total:
                    raise RuntimeError(
                        "from_pydata: face %d index %d is not a valid vertex index" % (position, slot)
                    )
        self.vertices = vertices
        self.polygons = [types.SimpleNamespace(vertices=list(face), loop_total=len(face))
                         for face in faces]
        return None

    def update(self):
        self._check_live()
        return None


class MeshesRegistry:
    """``bpy.data.meshes``: iterable, with Blender's ``new`` / ``remove``."""

    def __init__(self, blend):
        self._blend = blend
        self._items = []
        self.new_calls = 0
        self.remove_calls = 0

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def register(self, mesh):
        """Fixture builder: add a mesh without counting a bridge allocation."""
        self._items.append(mesh)
        return mesh

    def names(self):
        return [item.name for item in self._items if not item._removed]

    def get(self, name, default=None):
        for item in self._items:
            if item.name == name:
                return item
        return default

    def new(self, name):
        self.new_calls += 1
        final, suffix = name, 0
        taken = set(self.names())
        while final in taken:
            suffix += 1
            final = "%s.%03d" % (name, suffix)
        forced = self._blend.force_new_name
        if forced is not None:
            self._blend.force_new_name = None
            final = forced
        mesh = LiveMesh(self._blend, final)
        self._items.append(mesh)
        return mesh

    def remove(self, mesh, do_unlink=True, do_id_user=True):
        self.remove_calls += 1
        for index, item in enumerate(self._items):
            if item is mesh:
                self._items.pop(index)
                break
        else:
            raise RuntimeError("%r is not a mesh" % getattr(mesh, "_name", mesh))
        mesh._removed = True
        if do_unlink:
            for obj in self._blend.objects:
                if getattr(obj, "data", None) is mesh:
                    obj.data = None
        hook = self._blend.mesh_remove_hook
        if hook is not None:
            hook(mesh)


class LiveCollection(BaseCollection):
    """A collection whose name can be renamed the way Blender does (with ``.001``)."""

    def __init__(self, blend, name, library=None, override_library=None):
        super().__init__(blend, name, library=library, override_library=override_library)
        self.use_fake_user = False
        self._hide_viewport = False
        self._hide_render = False

    @property
    def name(self):
        self._check_live()
        return self._name

    @name.setter
    def name(self, value):
        self._check_live()
        final, suffix = value, 0
        while self._blend.name_taken(final, exclude=self):
            suffix += 1
            final = "%s.%03d" % (value, suffix)
        hook = self._blend.rename_hook
        if hook is not None and hook(self, value):
            raise RuntimeError("injected rename failure")
        forced = self._blend.force_stored_name
        if forced is not None:
            self._blend.force_stored_name = None
            final = forced
        self._name = final

    @property
    def hide_viewport(self):
        return self._hide_viewport

    @hide_viewport.setter
    def hide_viewport(self, value):
        if self._blend.ignored_writes.get("Collection.hide_viewport"):
            return
        self._hide_viewport = bool(value)

    @property
    def hide_render(self):
        return self._hide_render

    @hide_render.setter
    def hide_render(self, value):
        if self._blend.ignored_writes.get("Collection.hide_render"):
            return
        self._hide_render = bool(value)

    @property
    def users(self):
        blend = self._blend
        parents = len(blend.parents_of(self))
        fake = 1 if self.use_fake_user else 0
        return (parents + fake + blend.gn_reference_count(self)
                + blend.instance_reference_count(self) + blend.extra_users.get(id(self), 0))


class LiveObject(BaseObject):
    """An object with real name/RNA lifetime, data, references and hide state."""

    def __init__(self, blend, name, type="MESH", node_group=None, library=None,
                 override_library=None, data=None, parent=None):
        self._removed = False
        self._name = name
        self._hidden = {}
        self._hide_viewport = False
        super().__init__(blend, name, type=type, node_group=node_group, library=library,
                         override_library=override_library)
        self.type = type
        self.data = data
        self.parent = parent
        self.constraints = []
        self.vertex_groups = []
        self.animation_data = None
        self.instance_collection = None

    # -- RNA lifetime --------------------------------------------------- #
    def _check_live(self):
        if self._removed:
            raise ReferenceError("StructRNA of type Object has been removed")

    @property
    def name(self):
        self._check_live()
        return self._name

    @name.setter
    def name(self, value):
        final, suffix = value, 0
        taken = {item.name for item in self._blend.objects if item is not self}
        while final in taken:
            suffix += 1
            final = "%s.%03d" % (value, suffix)
        if getattr(self._blend, "rename_drops_reference", False) and self.constraints:
            # Models "the rename silently dropped a reference": the bridge must notice
            # through its before/after reference snapshot and restore the old name.
            self.constraints = []
        hook = self._blend.rename_hook
        if hook is not None and hook(self, value):
            raise RuntimeError("injected rename failure")
        forced = self._blend.force_stored_name
        if forced is not None:
            self._blend.force_stored_name = None
            final = forced
        self._name = final

    def as_pointer(self):
        self._check_live()
        return id(self)

    # -- display flags --------------------------------------------------- #
    @property
    def hide_viewport(self):
        return self._hide_viewport

    @hide_viewport.setter
    def hide_viewport(self, value):
        if self._blend.ignored_writes.get("Object.hide_viewport"):
            return
        self._hide_viewport = bool(value)

    @property
    def hide_render(self):
        return getattr(self, "_hide_render", False)

    @hide_render.setter
    def hide_render(self, value):
        if self._blend.ignored_writes.get("Object.hide_render"):
            return
        self._hide_render = bool(value)

    @staticmethod
    def _layer_name(view_layer):
        return getattr(view_layer, "name", None) if view_layer is not None else None

    def hide_set(self, hide, view_layer=None):
        self._check_live()
        self._hidden[self._layer_name(view_layer)] = bool(hide)

    def hide_get(self, view_layer=None):
        self._check_live()
        return bool(self._hidden.get(self._layer_name(view_layer), False))

    # -- users ----------------------------------------------------------- #
    @property
    def users(self):
        blend = self._blend
        owners = [collection for collection in blend.all_collections()
                  if any(item is self for item in collection.objects)]
        referenced = 0
        for other in blend.objects:
            if other is self:
                continue
            if getattr(other, "parent", None) is self:
                referenced += 1
            for constraint in list(getattr(other, "constraints", []) or []):
                if getattr(constraint, "target", None) is self or getattr(constraint, "object", None) is self:
                    referenced += 1
            for modifier in list(getattr(other, "modifiers", []) or []):
                if getattr(modifier, "object", None) is self:
                    referenced += 1
        for group in blend.node_groups:
            for node in list(getattr(group, "nodes", []) or []):
                if getattr(node, "object", None) is self:
                    referenced += 1
        return len(owners) + referenced + blend.extra_users.get(id(self), 0)


class ObjectsRegistry:
    """``bpy.data.objects``: iterable, with Blender's ``new`` / ``remove``."""

    def __init__(self, blend):
        self._blend = blend
        self._items = []
        self.new_calls = 0
        self.remove_calls = 0

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def append(self, obj):
        self._items.append(obj)

    def names(self):
        return [item.name for item in self._items if not item._removed]

    def get(self, name, default=None):
        for item in self._items:
            if item.name == name:
                return item
        return default

    def new(self, name, data=None):
        self.new_calls += 1
        final, suffix = name, 0
        taken = set(self.names())
        while final in taken:
            suffix += 1
            final = "%s.%03d" % (name, suffix)
        forced = self._blend.force_new_name
        if forced is not None:
            self._blend.force_new_name = None
            final = forced
        obj = LiveObject(self._blend, final, type="EMPTY" if data is None else "MESH", data=data)
        self._items.append(obj)
        return obj

    def remove(self, obj, do_unlink=True, do_id_user=True):
        self.remove_calls += 1
        for index, item in enumerate(self._items):
            if item is obj:
                self._items.pop(index)
                break
        else:
            raise RuntimeError("%r is not an object" % getattr(obj, "_name", obj))
        obj._removed = True
        if do_unlink:
            for collection in self._blend.all_collections():
                collection.objects._items = [entry for entry in collection.objects if entry is not obj]
        hook = self._blend.object_remove_hook
        if hook is not None:
            hook(obj)


class LiveCollectionsRegistry(BASE.CollectionsRegistry):
    """``bpy.data.collections`` creating renamable collections."""

    def new(self, name):
        self.new_calls += 1
        final, suffix = name, 0
        while self._blend.name_taken(final):
            suffix += 1
            final = "%s.%03d" % (name, suffix)
        forced = self._blend.force_new_name
        if forced is not None:
            self._blend.force_new_name = None
            final = forced
        collection = LiveCollection(self._blend, final)
        self._items.append(collection)
        return collection


class LiveScene(BaseScene):
    def __init__(self, blend, name, master=None, library=None, override_library=None):
        if master is None:
            master = LiveCollection(blend, name)
        super().__init__(blend, name, master=master, library=library,
                         override_library=override_library)
        self.view_layers = [ViewLayer(blend, self, "ViewLayer")]


class LiveFakeData(BASE.FakeData):
    """The extended fixture: same public shape as ``FakeData`` plus the new hooks."""

    def __init__(self):
        super().__init__()
        self.collections = LiveCollectionsRegistry(self)
        self.objects = ObjectsRegistry(self)
        self.meshes = MeshesRegistry(self)
        self.rename_hook = None
        self.object_remove_hook = None
        self.mesh_remove_hook = None
        self.force_stored_name = None
        self.extra_users = {}
        self.ignored_writes = {}

    # -- name/graph queries --------------------------------------------- #
    def name_taken(self, name, exclude=None):
        return any(getattr(block, "name", None) == name
                   for block in self.all_collections() if block is not exclude)

    def parents_of(self, collection):
        return [block for block in self.all_collections()
                if any(child is collection for child in block.children)]

    def gn_reference_count(self, collection):
        count = 0
        for group in self.node_groups:
            for node in list(getattr(group, "nodes", []) or []):
                if getattr(node, "collection", None) is collection:
                    count += 1
                    break
        return count

    def instance_reference_count(self, collection):
        return len([obj for obj in self.objects
                    if getattr(obj, "instance_collection", None) is collection])

    def user_map(self, subset=None):
        """``bpy.data.user_map``: the official ID -> users view."""
        mapping = {}
        for obj in (list(subset) if subset is not None else list(self.objects)):
            users = set()
            for owner in self.all_collections():
                if any(item is obj for item in owner.objects):
                    users.add(owner)
            for other in self.objects:
                if other is obj:
                    continue
                if getattr(other, "parent", None) is obj:
                    users.add(other)
                for constraint in list(getattr(other, "constraints", []) or []):
                    if getattr(constraint, "target", None) is obj or getattr(constraint, "object", None) is obj:
                        users.add(other)
                for modifier in list(getattr(other, "modifiers", []) or []):
                    if getattr(modifier, "object", None) is obj:
                        users.add(other)
            for group in self.node_groups:
                for node in list(getattr(group, "nodes", []) or []):
                    if getattr(node, "object", None) is obj:
                        users.add(group)
            mapping[obj] = users
        return mapping

    # -- fixture builders ------------------------------------------------ #
    def add_scene(self, name, master_name=None):
        scene = LiveScene(self, name, LiveCollection(self, master_name or name))
        self.scenes.append(scene)
        return scene

    def add_collection(self, name, parents=(), objects=(), library=None, override_library=None):
        collection = LiveCollection(self, name, library=library, override_library=override_library)
        self.collections._items.append(collection)
        for parent in parents:
            parent.children.link(collection)
        for obj in objects:
            collection.objects.link(obj)
        return collection

    def add_mesh(self, name):
        return self.meshes.register(LiveMesh(self, name))

    def add_object(self, name, collections=(), node_group=None, library=None,
                   override_library=None, type="MESH", data_name=None, parent=None,
                   modifier_object=None, instance_collection=None):
        data = self.add_mesh(data_name or (name + "_mesh")) if type == "MESH" else None
        if type == "MESH" and modifier_object is not None:
            node_group = node_group
        obj = LiveObject(self, name, type=type, node_group=node_group, library=library,
                         override_library=override_library, data=data, parent=parent)
        if modifier_object is not None:
            obj.modifiers.append(types.SimpleNamespace(type="NODES", node_group=None,
                                                       object=modifier_object))
        obj.instance_collection = instance_collection
        self.objects.append(obj)
        for collection in collections:
            collection.objects.link(obj)
        return obj

    def add_view_layer(self, scene, name):
        layer = ViewLayer(self, scene, name)
        scene.view_layers.append(layer)
        return layer

    def add_node_group(self, name, collection=None, object=None):
        nodes = []
        if collection is not None or object is not None:
            nodes.append(types.SimpleNamespace(collection=collection, object=object, name=name + "_Node"))
        group = types.SimpleNamespace(name=name, nodes=nodes)
        self.node_groups.append(group)
        return group


# --------------------------------------------------------------------------- #
# module loading (the stub is only needed while the modules import)
# --------------------------------------------------------------------------- #
_STUB = types.ModuleType("bpy")
_STUB.data = None
_PREVIOUS_BPY = MISSING


def _install_stub(blend):
    global _PREVIOUS_BPY
    _STUB.data = blend
    _PREVIOUS_BPY = sys.modules.get("bpy", MISSING)
    sys.modules["bpy"] = _STUB


def _remove_stub():
    global _PREVIOUS_BPY
    if _PREVIOUS_BPY is MISSING:
        sys.modules.pop("bpy", None)
    else:  # pragma: no cover - only when a real bpy is present
        sys.modules["bpy"] = _PREVIOUS_BPY


class _Registrar:
    """A fake ``api`` whose ``command`` decorator records the bridge commands."""

    def __init__(self):
        self.commands = {}

    def command(self, name, mutating=True, summary=""):
        def wrapped(func):
            self.commands[name] = func
            return func

        return wrapped


_install_stub(LiveFakeData())
try:
    OWNER = load_module(OUTLINER_PATH, "outliner_owner_under_test")
    OWNER_REGISTRAR = _Registrar()
    HELPERS = OWNER.register_commands(types.SimpleNamespace(CommandError=Failure,
                                                            command=OWNER_REGISTRAR.command))
    MANAGEMENT_COMMANDS, MANAGEMENT_REGISTRATION = {}, None
    MANAGEMENT = load_module(MANAGEMENT_PATH, "management_under_test")
    MANAGEMENT_REGISTRAR = _Registrar()
    MANAGEMENT_REGISTRATION = MANAGEMENT.register_commands(
        types.SimpleNamespace(CommandError=Failure, command=MANAGEMENT_REGISTRAR.command), HELPERS)
    MANAGEMENT_COMMANDS = MANAGEMENT_REGISTRAR.commands
finally:
    _remove_stub()

HELPERS_ARE_DICT = isinstance(HELPERS, dict)
HELPERS = HELPERS if HELPERS_ARE_DICT else {}
MISSING_HELPERS = [name for name in HELPER_KEYS if not callable(HELPERS.get(name))]
REGISTERED_COMMAND_NAMES = sorted(MANAGEMENT_COMMANDS)


class ManagementTest(unittest.TestCase):
    """Base fixture: one scene ``Main`` with two collections, two objects and a view layer."""

    def setUp(self):
        self.blend = LiveFakeData()
        _STUB.data = self.blend
        self.scene = self.blend.add_scene("Main")
        self.root = self.scene.collection
        self.layer = self.scene.view_layers[0]
        self.group_a = self.blend.add_node_group("GN_A")
        self.group_b = self.blend.add_node_group("GN_B")
        self.props = self.blend.add_collection("Props", parents=[self.root])
        self.assets = self.blend.add_collection("Assets", parents=[self.root])
        self.cube = self.blend.add_object("Cube", collections=[self.root, self.props],
                                          node_group=self.group_a)
        # A camera-like non-Mesh object: render visibility is Object.hide_render for it too.
        self.camera = self.blend.add_object("Cam", collections=[self.root], type="CAMERA")

    # -- call helpers --------------------------------------------------- #
    def load_bridge(self, module_name):
        """Load one more copy of the bridge module while the fake ``bpy`` is installed."""
        _install_stub(self.blend)
        try:
            return load_module(MANAGEMENT_PATH, module_name)
        finally:
            _remove_stub()

    def call(self, command, **params):
        return MANAGEMENT_COMMANDS["outliner_" + command](**params)

    def error(self, code, command, **params):
        with self.assertRaises(Failure) as caught:
            self.call(command, **params)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def collection_names(self):
        return sorted(self.blend.collections.names())

    def mesh_names(self):
        return sorted(self.blend.meshes.names())

    def object_names_now(self):
        return sorted(self.blend.objects.names())


# --------------------------------------------------------------------------- #
# contract: helpers are reused, nothing is re-implemented, no Blender/Project I/O
# --------------------------------------------------------------------------- #
class ContractTests(ManagementTest):
    def test_exactly_nine_commands_are_registered(self):
        self.assertEqual(REGISTERED_COMMAND_NAMES, EXPECTED_COMMANDS)
        self.assertEqual(sorted(MANAGEMENT_REGISTRATION["commands"]), EXPECTED_COMMANDS)
        for removed in REMOVED_INSTANCE_COMMANDS:
            self.assertNotIn(removed, MANAGEMENT_COMMANDS)

    def test_helper_dict_comes_from_the_owning_module(self):
        """``outliner.register_commands(api)`` returns its helper dict - no rewriting."""
        self.assertTrue(HELPERS_ARE_DICT, "blender/outliner.py must return its helper dict")
        self.assertEqual(MISSING_HELPERS, [],
                         "blender/outliner.py:register_commands must publish %s" % MISSING_HELPERS)
        self.assertTrue(set(HELPER_KEYS).issubset(set(MANAGEMENT_REGISTRATION["helpers"])))
        self.assertEqual(MANAGEMENT_REGISTRATION["object_types"], ["MESH", "EMPTY"])

    def test_incomplete_helpers_are_refused_loudly(self):
        partial = {name: value for name, value in HELPERS.items() if name != "guard"}
        registrar = _Registrar()
        api = types.SimpleNamespace(CommandError=Failure, command=registrar.command)
        module = self.load_bridge("management_missing_helper")
        with self.assertRaises(RuntimeError) as caught:
            module.register_commands(api, partial)
        self.assertIn("guard", str(caught.exception))
        self.assertEqual(registrar.commands, {})

    def test_source_has_no_ops_context_or_dynamic_code(self):
        source = MANAGEMENT_PATH.read_text(encoding="utf-8")
        for needle in ("bpy.context.", "bpy.ops.", "bpy.types.", "eval(", "exec("):
            self.assertNotIn(needle, source, "forbidden reference %r in the bridge module" % needle)
        self.assertIsNone(getattr(_STUB, "context", None))
        self.assertIsNone(getattr(_STUB, "ops", None))

    def test_modules_never_touch_the_filesystem_or_project(self):
        for path in (MANAGEMENT_PATH, TOOLS_PATH):
            source = path.read_text(encoding="utf-8")
            for needle in ("Project", "save_mainfile", "open(", "shutil", "sys.path", "subprocess"):
                self.assertNotIn(needle, source, "forbidden reference %r in %s" % (needle, path.name))
        self.assertNotIn("import bpy", TOOLS_PATH.read_text(encoding="utf-8"))

    def test_no_instance_api_survives_in_the_bridge(self):
        """Objects are ordinary Mesh/Empty objects; instances are only a safety scan."""
        source = MANAGEMENT_PATH.read_text(encoding="utf-8")
        code = source.split('"""', 2)[2]  # skip the module docstring
        self.assertIn("bpy.data.objects.new", code)
        self.assertIn("bpy.data.meshes", code)
        self.assertIn("from_pydata", code)
        self.assertNotIn("require_instance", code)
        self.assertNotIn("instance_type", code)
        self.assertNotIn("_transform", code)
        # The only instance reference left is the read-only safety scan of
        # collection_delete: a Collection Instance keeps its source collection alive.
        self.assertIn("instance_collection", code)
        self.assertIn("collection_instance_source", code)

    def test_test_process_is_hermetic(self):
        """The bridge only ever saw our stub, and the stub does not stay in ``sys.modules``."""
        self.assertIs(MANAGEMENT.__dict__.get("bpy"), _STUB)
        self.assertIsNot(sys.modules.get("bpy"), _STUB)
        self.assertFalse(hasattr(_STUB, "ops"))
        self.assertFalse(hasattr(_STUB, "context"))

    def test_fake_models_rna_invalidation_for_objects_and_meshes(self):
        victim = self.blend.add_object("Victim", collections=[self.root])
        victim_mesh = victim.data
        self.blend.objects.remove(victim)
        with self.assertRaises(ReferenceError):
            victim.name  # noqa: B018 - the access itself is the assertion
        self.assertEqual(victim_mesh.name, "Victim_mesh")  # data survives the object
        self.blend.meshes.remove(victim_mesh)
        with self.assertRaises(ReferenceError):
            victim_mesh.name  # noqa: B018
        self.assertEqual(self.blend.meshes.remove_calls, 1)


# --------------------------------------------------------------------------- #
# collection create / rename / move
# --------------------------------------------------------------------------- #
class CollectionCreateTests(ManagementTest):
    def test_create_links_one_datablock_and_touches_nothing_else(self):
        before = (self.cube.snapshot(), [group.name for group in self.blend.node_groups])
        result = self.call("collection_create", scene="Main", parent_path="Props", name="Sub")
        self.assertEqual(result["created"], ["Sub"])
        self.assertEqual(result["reused"], [])
        self.assertTrue(result["changed"])
        self.assertFalse(result["idempotent"])
        self.assertIs(result["auto_suffix"], False)
        self.assertEqual(result["path"], "Props/Sub")
        self.assertEqual(result["collection"]["path"], "Props/Sub")
        self.assertEqual(self.blend.collections.new_calls, 1)
        self.assertEqual(self.collection_names(), ["Assets", "Props", "Sub"])
        self.assertEqual([child.name for child in self.props.children], ["Sub"])
        self.assertEqual((self.cube.snapshot(), [g.name for g in self.blend.node_groups]), before)

    def test_existing_same_location_is_reused_never_suffixed(self):
        self.call("collection_create", scene="Main", parent_path="", name="Shared")
        second = self.call("collection_create", scene="Main", parent_path="", name="Shared")
        self.assertEqual(second["created"], [])
        self.assertEqual(second["reused"], ["Shared"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(self.blend.collections.new_calls, 1)
        self.assertFalse(any(".001" in name for name in self.collection_names()))

    def test_same_name_elsewhere_is_refused_without_suffix(self):
        caught = self.error("collection_name_conflict", "collection_create",
                            scene="Main", parent_path="Assets", name="Props")
        self.assertEqual(caught.details["reason"], "exists_elsewhere")
        self.assertIs(caught.details["auto_suffix"], False)
        self.assertEqual(self.blend.collections.new_calls, 0)
        self.assertEqual(list(self.assets.children), [])

    def test_invalid_names_and_parents_are_refused_before_any_allocation(self):
        for bad in ("", "   ", "A/B", "A\\B", "A" * 256, None, 7):
            self.error("invalid_name", "collection_create", scene="Main", parent_path="", name=bad)
        self.error("collection_not_found", "collection_create",
                   scene="Main", parent_path="Ghost", name="New")
        self.error("invalid_path", "collection_create", scene="Main", parent_path="/Props", name="New")
        self.assertEqual(self.blend.collections.new_calls, 0)

    def test_parent_and_name_guards(self):
        other = self.blend.add_scene("Other")
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        multi = self.blend.add_collection("Multi", parents=[self.root, self.props])
        self.error("read_only_collection", "collection_create",
                   scene="Main", parent_path="Linked", name="New")
        self.error("collection_shared_across_scenes", "collection_create",
                   scene="Main", parent_path="Shared", name="New")
        self.error("collection_multi_parent", "collection_create",
                   scene="Main", parent_path="Multi", name="New")
        self.blend.add_scene("Taken")
        self.error("collection_name_conflict", "collection_create",
                   scene="Main", parent_path="", name="Taken")
        self.assertEqual(self.blend.collections.new_calls, 0)

    def test_renamed_allocation_is_detected_and_removed(self):
        self.blend.force_new_name = "New.001"
        caught = self.error("invalid_name", "collection_create",
                            scene="Main", parent_path="", name="New")
        self.assertEqual(caught.details["cleanup"]["collection"], "New.001")
        self.assertIs(caught.details["cleanup"]["removed"], True)
        self.assertEqual(self.blend.collections.remove_calls, 1)
        self.assertEqual(self.collection_names(), ["Assets", "Props"])

    def test_link_failure_removes_only_the_new_collection(self):
        self.blend.link_hook = lambda owner, item, kind: (
            kind == "children" and getattr(item, "name", None) == "Sub")
        caught = self.error("outliner_write_failed", "collection_create",
                            scene="Main", parent_path="Props", name="Sub")
        self.assertEqual(caught.details["cleanup"]["collection"], "Sub")
        self.assertEqual(self.blend.collections.remove_calls, 1)
        self.assertEqual(list(self.props.children), [])


class CollectionRenameTests(ManagementTest):
    def test_rename_keeps_membership_and_is_idempotent(self):
        before = self.cube.snapshot()
        result = self.call("collection_rename", scene="Main", path="Props", name="Skins")
        self.assertEqual(result["previous_name"], "Props")
        self.assertEqual(result["new_path"], "Skins")
        self.assertIs(result["auto_suffix"], False)
        self.assertEqual([collection.name for collection in self.cube.users_collection],
                         ["Main", "Skins"])
        self.assertEqual(self.cube.snapshot(), before)
        repeat = self.call("collection_rename", scene="Main", path="Skins", name="Skins")
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])

    def test_conflict_root_and_guards(self):
        caught = self.error("collection_name_conflict", "collection_rename",
                            scene="Main", path="Props", name="Assets")
        self.assertIs(caught.details["auto_suffix"], False)
        self.assertEqual(self.props.name, "Props")
        self.error("root_immutable", "collection_rename", scene="Main", path="", name="Renamed")
        other = self.blend.add_scene("Other")
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        multi = self.blend.add_collection("Multi", parents=[self.root, self.props])
        self.error("read_only_collection", "collection_rename", scene="Main", path="Linked", name="X")
        self.error("collection_shared_across_scenes", "collection_rename",
                   scene="Main", path="Shared", name="X")
        self.error("collection_multi_parent", "collection_rename", scene="Main", path="Multi", name="X")
        self.assertEqual(linked.name, "Linked")

    def test_failure_and_stored_drift_restore_the_previous_name(self):
        self.blend.rename_hook = lambda block, value: getattr(block, "name", None) == "Props"
        self.error("outliner_write_failed", "collection_rename", scene="Main", path="Props", name="Skins")
        self.assertEqual(self.props.name, "Props")
        self.blend.rename_hook = None
        self.blend.force_stored_name = "Skins.001"
        caught = self.error("invalid_name", "collection_rename", scene="Main", path="Props", name="Skins")
        self.assertEqual(caught.details["stored_name"], "Skins.001")
        self.assertEqual(caught.details["restored_name"], "Props")
        self.assertEqual(self.props.name, "Props")


class CollectionMoveTests(ManagementTest):
    def test_move_relinks_without_touching_objects_or_data(self):
        before = (self.cube.snapshot(), [group.name for group in self.blend.node_groups])
        result = self.call("collection_move", scene="Main", path="Props", parent_path="Assets")
        self.assertEqual(result["previous_parent"], "Main")
        self.assertEqual(result["new_path"], "Assets/Props")
        self.assertEqual([child.name for child in self.root.children], ["Assets"])
        self.assertEqual([child.name for child in self.assets.children], ["Props"])
        self.assertEqual(result["objects_preserved"], ["Cube"])
        self.assertEqual((self.cube.snapshot(), [g.name for g in self.blend.node_groups]), before)

    def test_move_to_the_root_and_repeat_is_idempotent(self):
        self.blend.add_collection("Inner", parents=[self.props])
        result = self.call("collection_move", scene="Main", path="Props/Inner", parent_path="")
        self.assertEqual(result["new_path"], "Inner")
        repeat = self.call("collection_move", scene="Main", path="Inner", parent_path="")
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])

    def test_cycles_and_root_are_refused_before_any_write(self):
        self.blend.add_collection("Deep", parents=[self.props])
        self.error("cycle_hierarchy", "collection_move", scene="Main", path="Props", parent_path="Props")
        self.error("cycle_hierarchy", "collection_move", scene="Main", path="Props",
                   parent_path="Props/Deep")
        self.error("root_immutable", "collection_move", scene="Main", path="", parent_path="Props")
        self.assertEqual([child.name for child in self.root.children], ["Props", "Assets"])

    def test_failed_steps_are_compensated_and_reported(self):
        self.blend.link_hook = lambda owner, item, kind: (
            kind == "children" and getattr(item, "name", None) == "Props")
        caught = self.error("outliner_write_failed", "collection_move", scene="Main",
                            path="Props", parent_path="Assets")
        self.assertTrue(caught.details["rolled_back"])
        self.blend.link_hook = None
        self.assertEqual([child.name for child in self.root.children], ["Props", "Assets"])
        self.assertEqual([child.name for child in self.assets.children], [])
        self.blend.unlink_hook = lambda owner, item, kind: (
            kind == "children" and getattr(item, "name", None) == "Props"
            and getattr(owner, "name", None) == "Main")
        caught = self.error("outliner_write_failed", "collection_move", scene="Main",
                            path="Props", parent_path="Assets")
        self.assertEqual(caught.details["compensated"], ["link Props under Assets"])
        self.assertEqual([child.name for child in self.root.children], ["Props", "Assets"])


# --------------------------------------------------------------------------- #
# collection delete
# --------------------------------------------------------------------------- #
class CollectionDeleteTests(ManagementTest):
    def test_confirmation_and_children_gates(self):
        caught = self.error("confirmation_required", "collection_delete",
                            scene="Main", path="Props", confirm_delete=False)
        self.assertIs(caught.details["confirm_delete"], False)
        self.error("invalid_argument", "collection_delete", scene="Main", path="Props",
                   confirm_delete="yes")
        self.blend.add_collection("Deep", parents=[self.props])
        caught = self.error("collection_has_children", "collection_delete",
                            scene="Main", path="Props", confirm_delete=True)
        self.assertEqual(caught.details["children"], ["Deep"])
        self.assertEqual(self.blend.collections.remove_calls, 0)

    def test_leaf_delete_keeps_parent_and_siblings(self):
        result = self.call("collection_delete", scene="Main", path="Props", confirm_delete=True)
        self.assertEqual(result["collections_deleted"], ["Props"])
        self.assertEqual(result["objects_deleted"], [])
        self.assertIs(result["not_transactional"], True)
        self.assertIs(result["partial"], False)
        self.assertEqual(self.collection_names(), ["Assets"])
        self.assertEqual(result["siblings_kept"], ["Assets"])
        self.assertEqual(result["parent"], "Main")

    def test_recursive_delete_covers_exactly_the_selected_subtree(self):
        deep = self.blend.add_collection("Deep", parents=[self.props])
        leaf = self.blend.add_collection("Leaf", parents=[deep])
        self.blend.add_collection("Keep", parents=[deep])
        self.blend.add_collection("Sibling", parents=[self.root])
        deep_cube = self.blend.add_object("DeepCube", collections=[deep, self.root])
        result = self.call("collection_delete", scene="Main", path="Props",
                           confirm_delete=True, recursive=True)
        self.assertEqual(result["collections_deleted"], ["Leaf", "Keep", "Deep", "Props"])
        self.assertEqual(sorted(result["subtree"]), ["Deep", "Keep", "Leaf", "Props"])
        self.assertEqual(self.collection_names(), ["Assets", "Sibling"])
        self.assertIs(self.blend.object("DeepCube"), deep_cube)
        self.assertEqual(self.blend.objects.remove_calls, 0)
        for removed in (deep, leaf, self.props):
            with self.assertRaises(ReferenceError):
                removed.name  # noqa: B018 - removed datablocks have no readable RNA
        self.assertEqual(sorted(result["objects_preserved"]), ["Cube", "DeepCube"])
        self.assertEqual(sorted(entry["collection"] for entry in result["parent_names"]),
                         ["Deep", "Keep", "Leaf", "Props"])
        self.assertEqual({entry["collection"]: entry["parents"] for entry in result["parent_names"]},
                         {"Deep": ["Props"], "Leaf": ["Deep"], "Keep": ["Deep"], "Props": ["Main"]})

    def test_objects_that_would_leave_the_scene_are_linked_to_the_root(self):
        detached = self.blend.add_object("Detached", collections=[self.props])
        before = detached.snapshot()
        result = self.call("collection_delete", scene="Main", path="Props",
                           confirm_delete=True, recursive=True)
        self.assertEqual(result["relinked_to_root"], ["Detached"])
        self.assertEqual([collection.name for collection in detached.users_collection], ["Main"])
        self.assertEqual(detached.snapshot(), before)
        self.assertTrue(any("scene root" in warning for warning in result["warnings"]))

    def test_an_object_linked_in_several_subtree_collections_is_planned_once(self):
        deep = self.blend.add_collection("Deep", parents=[self.props])
        both = self.blend.add_object("Both", collections=[self.props, deep], node_group=self.group_a)
        result = self.call("collection_delete", scene="Main", path="Props",
                           confirm_delete=True, recursive=True)
        self.assertEqual(result["relinked_to_root"], ["Both"])
        self.assertEqual([entry["object"] for entry in result["preserved"]].count("Both"), 1)
        self.assertEqual([collection.name for collection in both.users_collection], ["Main"])
        self.assertIs(self.blend.object("Both"), both)

    def test_objects_and_geometry_nodes_are_preserved(self):
        before = self.cube.snapshot()
        self.call("collection_delete", scene="Main", path="Props", confirm_delete=True)
        self.assertEqual([group.name for group in self.blend.node_groups], ["GN_A", "GN_B"])
        self.assertEqual(self.cube.snapshot(), before)
        self.assertEqual(self.cube.data.name, "Cube_mesh")
        self.assertEqual(self.mesh_names(), ["Cube_mesh"])

    def test_shared_object_stays_in_target_scene_after_collection_deletion(self):
        other = self.blend.add_scene("Other")
        remote = self.blend.add_collection("Remote", parents=[other.collection])
        both = self.blend.add_object("BothScenes", collections=[self.props, remote])
        result = self.call("collection_delete", scene="Main", path="Props", confirm_delete=True)
        self.assertIn("BothScenes", result["relinked_to_root"])
        self.assertEqual({owner.name for owner in both.users_collection}, {"Main", "Remote"})

    def test_instance_source_geometry_nodes_fake_user_and_users_refuse_the_delete(self):
        instance = self.blend.add_object("Inst", collections=[self.root], type="EMPTY",
                                        instance_collection=self.props)
        caught = self.error("collection_in_use", "collection_delete",
                            scene="Main", path="Props", confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "collection_instance_source")
        self.assertEqual(caught.details["problems"][0]["objects"], ["Inst"])
        self.assertIs(self.blend.object("Inst"), instance)
        instance.instance_collection = None
        self.blend.add_node_group("GN_Use", collection=self.assets)
        caught = self.error("collection_in_use", "collection_delete",
                            scene="Main", path="Assets", confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "geometry_nodes_reference")
        self.assets.use_fake_user = True
        self.blend.extra_users[id(self.assets)] = 1
        caught = self.error("collection_in_use", "collection_delete",
                            scene="Main", path="Assets", confirm_delete=True)
        self.assertEqual({problem["reason"] for problem in caught.details["problems"]},
                         {"fake_user", "geometry_nodes_reference", "external_users"})
        self.assertEqual(self.blend.collections.remove_calls, 0)

    def test_shared_multi_parent_linked_and_root_are_refused(self):
        other = self.blend.add_scene("Other")
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        multi = self.blend.add_collection("Multi", parents=[self.root, self.props])
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        self.error("root_immutable", "collection_delete", scene="Main", path="", confirm_delete=True)
        self.error("collection_shared_across_scenes", "collection_delete", scene="Main",
                   path="Shared", confirm_delete=True)
        self.error("collection_multi_parent", "collection_delete", scene="Main",
                   path="Multi", confirm_delete=True)
        self.error("read_only_collection", "collection_delete", scene="Main",
                   path="Linked", confirm_delete=True)
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertIs(self.blend.collection("Shared"), shared)

    def test_partial_removal_is_reported_honestly(self):
        self.blend.add_collection("Deep", parents=[self.props])
        self.blend.add_collection("Leaf", parents=[self.props])
        calls = {"n": 0}

        def remove_hook(collection):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected remove failure")

        self.blend.remove_hook = remove_hook
        caught = self.error("outliner_delete_partial", "collection_delete", scene="Main",
                            path="Props", confirm_delete=True, recursive=True)
        self.assertEqual(len(caught.details["deleted"]), 1)
        self.assertIs(caught.details["partial"], True)
        self.assertIs(caught.details["not_transactional"], True)
        self.assertIs(caught.details["rolled_back"], False)
        self.assertIn("injected remove failure", caught.details["error"])
        self.assertEqual(sorted(caught.details["residual"]), sorted(caught.details["remaining"]))
        self.assertEqual(sorted(self.blend.collections.names()), ["Assets", "Props"])

    def test_relink_failure_stops_before_any_removal(self):
        detached = self.blend.add_object("Detached", collections=[self.props])
        self.blend.link_hook = lambda owner, item, kind: (
            kind == "objects" and getattr(item, "name", None) == "Detached")
        caught = self.error("outliner_write_failed", "collection_delete", scene="Main",
                            path="Props", confirm_delete=True)
        self.assertTrue(caught.details["rolled_back"])
        self.assertEqual(self.blend.collections.remove_calls, 0)
        self.assertEqual([collection.name for collection in detached.users_collection], ["Props"])

    def test_all_payloads_are_json_serializable(self):
        self.call("collection_create", scene="Main", parent_path="Props", name="Sub")
        payloads = [
            self.call("collection_create", scene="Main", parent_path="Props", name="Sub2"),
            self.call("collection_rename", scene="Main", path="Props/Sub", name="SubA"),
            self.call("collection_move", scene="Main", path="Props/SubA", parent_path="Assets"),
            self.call("collection_delete", scene="Main", path="Props/Sub2", confirm_delete=True),
        ]
        for index, payload in enumerate(payloads):
            self.assertIsInstance(json.loads(json.dumps(payload)), dict, "payload %d" % index)


# --------------------------------------------------------------------------- #
# collection display: eye / checkbox / monitor / render
# --------------------------------------------------------------------------- #
class CollectionDisplayTests(ManagementTest):
    def test_every_switch_toggles_true_and_false_and_stays_independent(self):
        node = self.layer.layer_for(self.props)
        for flag in ("hidden", "exclude", "disable_viewport", "hide_render"):
            on = self.call("collection_display", scene="Main", path="Props",
                           view_layer="ViewLayer", **{flag: True})
            self.assertEqual(on["requested"], {flag: True})
            self.assertTrue(on["after"][flag], flag)
            self.assertTrue(on["changed"])
            off = self.call("collection_display", scene="Main", path="Props",
                            view_layer="ViewLayer", **{flag: False})
            self.assertFalse(off["after"][flag], flag)
            self.assertTrue(off["changed"])
        # Four different properties: the eye and the checkbox live on the view layer,
        # the monitor and the render flag on the collection datablock.
        self.call("collection_display", scene="Main", path="Props", view_layer="ViewLayer",
                  hidden=True, exclude=True, disable_viewport=True, hide_render=True)
        self.assertTrue(node.hide_viewport and node.exclude)
        self.assertTrue(self.props.hide_viewport and self.props.hide_render)
        self.assertEqual(self.call("collection_display", scene="Main", path="Props",
                                   view_layer="ViewLayer", hidden=False)["after"], {"hidden": False})
        self.assertTrue(node.exclude and self.props.hide_viewport and self.props.hide_render)
        payload = self.call("collection_display", scene="Main", path="Props", view_layer="ViewLayer",
                            disable_viewport=False)
        self.assertEqual(payload["view_layer_flags"], ["exclude", "hidden"])
        self.assertEqual(payload["datablock_flags"], ["disable_viewport", "hide_render"])
        self.assertTrue(payload["monitor_supported"])
        self.assertTrue(any("datablock-wide" in warning for warning in payload["warnings"]))

    def test_the_eye_is_per_view_layer_while_the_monitor_is_global(self):
        second = self.blend.add_view_layer(self.scene, "Second")
        self.call("collection_display", scene="Main", path="Props", view_layer="Second", hidden=True)
        self.assertTrue(second.layer_for(self.props).hide_viewport)
        self.assertFalse(self.layer.layer_for(self.props).hide_viewport)
        self.call("collection_display", scene="Main", path="Props", view_layer="Second",
                  disable_viewport=True)
        self.assertTrue(self.props.hide_viewport)
        self.assertFalse(self.layer.layer_for(self.props).hide_viewport)

    def test_all_null_non_boolean_and_missing_view_layer(self):
        self.error("invalid_argument", "collection_display", scene="Main", path="Props",
                   view_layer="ViewLayer")
        self.error("invalid_argument", "collection_display", scene="Main", path="Props",
                   view_layer="ViewLayer", disable_viewport=1)
        self.error("invalid_argument", "collection_display", scene="Main", path="Props",
                   view_layer="ViewLayer", exclude="yes")
        self.error("view_layer_not_found", "collection_display", scene="Main", path="Props",
                   view_layer="Ghost", hidden=True)
        self.error("invalid_view_layer", "collection_display", scene="Main", path="Props",
                   view_layer="", hidden=True)
        self.blend.add_view_layer(self.scene, "Second")
        self.blend.add_view_layer(self.scene, "Second")
        self.error("ambiguous_view_layer", "collection_display", scene="Main", path="Props",
                   view_layer="Second", hidden=True)
        self.assertFalse(self.props.hide_viewport)

    def test_datablock_switches_use_the_single_owner_guard(self):
        other = self.blend.add_scene("Other")
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        multi = self.blend.add_collection("Multi", parents=[self.root, self.props])
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        for flag in ("disable_viewport", "hide_render"):
            self.error("collection_shared_across_scenes", "collection_display", scene="Main",
                       path="Shared", view_layer="ViewLayer", **{flag: True})
            self.error("collection_multi_parent", "collection_display", scene="Main",
                       path="Multi", view_layer="ViewLayer", **{flag: True})
            self.error("read_only_collection", "collection_display", scene="Main",
                       path="Linked", view_layer="ViewLayer", **{flag: True})
        self.assertFalse(shared.hide_viewport or shared.hide_render)
        self.assertFalse(multi.hide_viewport or multi.hide_render)
        self.assertFalse(linked.hide_viewport or linked.hide_render)
        # The per-view-layer eye and checkbox only need the collection in that layer.
        result = self.call("collection_display", scene="Main", path="Shared",
                           view_layer="ViewLayer", hidden=True, exclude=True)
        self.assertTrue(self.layer.layer_for(shared).hide_viewport)
        self.assertTrue(self.layer.layer_for(shared).exclude)
        self.assertEqual(result["collection"]["scenes"], ["Main", "Other"])

    def test_display_payloads_are_json_serializable(self):
        collection_payload = self.call("collection_display", scene="Main", path="Props",
                                       view_layer="ViewLayer", hidden=True, disable_viewport=True,
                                       hide_render=True, exclude=True)
        object_payload = self.call("object_display", scene="Main", objects=["Cube", "Cam"],
                                   view_layer="ViewLayer", hidden=True, disable_viewport=True,
                                   hide_render=True)
        for payload in (collection_payload, object_payload):
            self.assertIsInstance(json.loads(json.dumps(payload)), dict)
        self.assertEqual(sorted(collection_payload["requested"]),
                         ["disable_viewport", "exclude", "hidden", "hide_render"])
        self.assertEqual(object_payload["objects"], ["Cube", "Cam"])

    def test_a_layer_that_wraps_another_collection_is_refused(self):
        other = self.blend.add_scene("Other", master_name="Props")
        foreign = ViewLayer(self.blend, other, "Foreign")
        self.scene.view_layers.append(foreign)
        caught = self.error("collection_not_in_view_layer", "collection_display", scene="Main",
                            path="", view_layer="Foreign", hidden=True)
        self.assertEqual(caught.details["view_layer"], "Foreign")
        self.assertFalse(foreign.layer_for(other.collection).hide_viewport)

    def test_ignored_write_is_detected_and_restored_for_each_scope(self):
        for key, flag in (("LayerCollection.hide_viewport", "hidden"),
                          ("Collection.hide_viewport", "disable_viewport"),
                          ("Collection.hide_render", "hide_render")):
            self.blend.ignored_writes.clear()
            self.blend.ignored_writes[key] = True
            caught = self.error("outliner_write_failed", "collection_display", scene="Main",
                                path="Props", view_layer="ViewLayer", **{flag: True})
            self.assertEqual(caught.details["after"], {flag: False})
            self.assertEqual(caught.details["restored"], {flag: False})
        self.blend.ignored_writes.clear()
        self.assertFalse(self.props.hide_viewport or self.props.hide_render)
        self.assertFalse(self.layer.layer_for(self.props).hide_viewport)


# --------------------------------------------------------------------------- #
# object display: eye / monitor / render
# --------------------------------------------------------------------------- #
class ObjectDisplayTests(ManagementTest):
    def test_every_switch_toggles_true_and_false_and_stays_independent(self):
        for flag in ("hidden", "disable_viewport", "hide_render"):
            on = self.call("object_display", scene="Main", objects=["Cube"],
                           view_layer="ViewLayer", **{flag: True})
            self.assertEqual(on["requested"], {flag: True})
            self.assertTrue(on["after"]["Cube"][flag], flag)
            self.assertTrue(on["changed"])
            off = self.call("object_display", scene="Main", objects=["Cube"],
                            view_layer="ViewLayer", **{flag: False})
            self.assertFalse(off["after"]["Cube"][flag], flag)
        self.assertFalse(self.cube.hide_viewport)
        self.assertFalse(self.cube.hide_get(view_layer=self.layer))
        self.assertFalse(self.cube.hide_render)
        payload = self.call("object_display", scene="Main", objects=["Cube"],
                            view_layer="ViewLayer", disable_viewport=True)
        self.assertEqual(payload["view_layer_flags"], ["hidden"])
        self.assertEqual(payload["datablock_flags"], ["disable_viewport", "hide_render"])
        self.assertTrue(payload["monitor_supported"])
        self.assertTrue(any("datablock-wide" in warning for warning in payload["warnings"]))

    def test_the_eye_is_per_view_layer_while_the_monitor_is_global(self):
        second = self.blend.add_view_layer(self.scene, "Second")
        self.call("object_display", scene="Main", objects=["Cube"], view_layer="Second", hidden=True)
        self.assertTrue(self.cube.hide_get(view_layer=second))
        self.assertFalse(self.cube.hide_get(view_layer=self.layer))
        self.call("object_display", scene="Main", objects=["Cube"], view_layer="Second",
                  hide_render=True)
        self.assertTrue(self.cube.hide_render)
        self.assertFalse(self.cube.hide_get(view_layer=self.layer))

    def test_all_null_non_boolean_and_batch_validation(self):
        self.error("invalid_argument", "object_display", scene="Main", objects=["Cube"],
                   view_layer="ViewLayer")
        self.error("invalid_argument", "object_display", scene="Main", objects=["Cube"],
                   view_layer="ViewLayer", hidden="yes")
        self.error("object_not_found", "object_display", scene="Main",
                   objects=["Cube", "Ghost"], view_layer="ViewLayer", hidden=True)
        self.error("duplicate_object", "object_display", scene="Main", objects=["Cube", "Cube"],
                   view_layer="ViewLayer", hidden=True)
        self.error("invalid_objects", "object_display", scene="Main", objects=[],
                   view_layer="ViewLayer", hidden=True)
        self.assertFalse(self.cube.hide_get(view_layer=self.layer))
        self.assertFalse(self.cube.hide_viewport)

    def test_scene_membership_shared_and_linked_objects_are_refused(self):
        loose = self.blend.add_object("Loose", collections=[])
        other = self.blend.add_scene("Other")
        shared_cube = self.blend.add_object("SharedCube", collections=[self.root, other.collection])
        linked = self.blend.add_object("LinkedObj", collections=[self.root], library=object())
        self.error("object_not_in_scene", "object_display", scene="Main", objects=["Loose"],
                   view_layer="ViewLayer", disable_viewport=True)
        self.error("object_shared_across_scenes", "object_display", scene="Main",
                   objects=["SharedCube"], view_layer="ViewLayer", hide_render=True)
        self.error("read_only_object", "object_display", scene="Main", objects=["LinkedObj"],
                   view_layer="ViewLayer", hidden=True)
        self.assertIs(self.blend.object("Loose"), loose)
        self.assertIs(self.blend.object("SharedCube"), shared_cube)
        self.assertIs(self.blend.object("LinkedObj"), linked)

    def test_missing_hide_api_and_ignored_writes(self):
        self.cube.hide_set = None
        self.error("api_unsupported", "object_display", scene="Main", objects=["Cube"],
                   view_layer="ViewLayer", hidden=True)
        self.call("object_display", scene="Main", objects=["Cube"], view_layer="ViewLayer",
                  disable_viewport=True)
        self.assertTrue(self.cube.hide_viewport)
        self.blend.ignored_writes["Object.hide_viewport"] = True
        caught = self.error("outliner_write_failed", "object_display", scene="Main",
                            objects=["Cube"], view_layer="ViewLayer", disable_viewport=False)
        self.assertEqual(caught.details["after"]["Cube"]["disable_viewport"], True)
        self.assertEqual(caught.details["restored"]["Cube.disable_viewport"], True)

    def test_display_never_touches_data_modifiers_or_groups(self):
        data = self.cube.data
        self.call("object_display", scene="Main", objects=["Cube"], view_layer="ViewLayer",
                  hidden=True, disable_viewport=True, hide_render=True)
        self.assertIs(self.cube.data, data)
        self.assertEqual(self.cube.data.name, "Cube_mesh")
        self.assertEqual([(modifier.type, modifier.node_group) for modifier in self.cube.modifiers],
                         [("NODES", self.group_a)])
        self.assertEqual([group.name for group in self.blend.node_groups], ["GN_A", "GN_B"])
        self.assertIs(self.cube.modifiers[0].node_group, self.group_a)
        self.assertEqual(self.blend.meshes.remove_calls, 0)


# --------------------------------------------------------------------------- #
# object create
# --------------------------------------------------------------------------- #
class ObjectCreateTests(ManagementTest):
    def test_two_ordinary_mesh_objects_are_created(self):
        first = self.call("object_create", scene="Main", path="Props", name="PanelA",
                          vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
        second = self.call("object_create", scene="Main", path="Props", name="PanelB",
                           type="MESH", vertices=[[0, 0, 0], [2, 0, 0], [0, 2, 0]],
                           faces=[[0, 1, 2]])
        a = self.blend.object("PanelA")
        b = self.blend.object("PanelB")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNot(a, b)
        for payload, obj, expected_vertices in ((first, a, 3), (second, b, 3)):
            self.assertEqual(obj.type, "MESH")
            self.assertEqual(payload["type"], "MESH")
            self.assertEqual(payload["object"]["data"], obj.name)
            self.assertEqual(payload["vertices"], expected_vertices)
            self.assertEqual(payload["faces"], 1)
            self.assertEqual(payload["path"], "Props")
            self.assertEqual([collection.name for collection in obj.users_collection], ["Props"])
            self.assertIsNone(payload["cleanup"])
            self.assertTrue(payload["created"])
            self.assertEqual(payload["membership"]["scenes"], ["Main"])
        self.assertEqual(a.data.name, "PanelA")
        self.assertEqual(len(a.data.vertices), 3)
        self.assertEqual(len(a.data.polygons), 1)
        self.assertEqual(a.data.polygons[0].vertices, [0, 1, 2])
        self.assertEqual(self.blend.objects.new_calls, 2)
        self.assertEqual(self.blend.meshes.new_calls, 2)
        self.assertIn("PanelA", self.mesh_names())
        self.assertIn("PanelB", self.mesh_names())

    def test_default_mesh_is_empty_and_ready_for_geometry_nodes(self):
        result = self.call("object_create", scene="Main", path="", name="Procedural")
        obj = self.blend.object("Procedural")
        self.assertEqual(obj.type, "MESH")
        self.assertEqual(result["vertices"], 0)
        self.assertEqual(result["faces"], 0)
        self.assertEqual(obj.data.vertices, [])
        self.assertTrue(any("empty MESH" in warning for warning in result["warnings"]))

    def test_empty_type_has_no_data_block(self):
        result = self.call("object_create", scene="Main", path="Props", name="Anchor", type="EMPTY")
        obj = self.blend.object("Anchor")
        self.assertEqual(obj.type, "EMPTY")
        self.assertIsNone(obj.data)
        self.assertIsNone(result["data"])
        self.assertEqual(result["vertices"], 0)
        self.assertEqual(self.blend.meshes.new_calls, 0)
        self.assertEqual(self.blend.objects.new_calls, 1)

    def test_type_and_geometry_validation_happens_before_any_allocation(self):
        self.error("invalid_type", "object_create", scene="Main", path="", name="X", type="CURVE")
        self.error("invalid_type", "object_create", scene="Main", path="", name="X", type=None)
        self.error("invalid_argument", "object_create", scene="Main", path="", name="X",
                   type="EMPTY", vertices=[[0, 0, 0]])
        self.error("invalid_argument", "object_create", scene="Main", path="", name="X",
                   type="EMPTY", faces=[[0, 1, 2]])
        for bad in ("0,0,0", 1, [[0, 0, 0], [1, 1]], [[0, 0]], [[0, 0, 0, 0]],
                    [[0, 0, float("nan")]], [[0, 0, float("inf")]], [[0, True, 0]], [["a", 0, 0]]):
            self.error("invalid_vertices", "object_create", scene="Main", path="", name="X",
                       vertices=bad)
        for bad in ("012", 1, [[0, 1]], [[0, 1, 3]], [[0, 1, -1]], [[0, 1, 1.0]],
                    [[0, 1, True]], [["0", 1, 2]], [0, 1, 2]):
            self.error("invalid_faces", "object_create", scene="Main", path="", name="X",
                       vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=bad)
        self.error("invalid_faces", "object_create", scene="Main", path="", name="X",
                   faces=[[0, 1, 2]])
        self.error("invalid_name", "object_create", scene="Main", path="", name="")
        self.assertEqual(self.blend.objects.new_calls, 0)
        self.assertEqual(self.blend.meshes.new_calls, 0)
        self.assertEqual(self.object_names_now(), ["Cam", "Cube"])

    def test_name_and_data_name_conflicts_are_prechecked_without_suffix(self):
        caught = self.error("object_name_conflict", "object_create", scene="Main", path="",
                            name="Cube")
        self.assertEqual(caught.details["existing"][0]["object"], "Cube")
        self.assertIs(caught.details["auto_suffix"], False)
        self.blend.add_mesh("FreeMesh")
        caught = self.error("data_name_conflict", "object_create", scene="Main", path="",
                            name="FreeMesh")
        self.assertEqual(caught.details["reason"], "mesh_name_exists_elsewhere")
        self.assertIs(caught.details["auto_suffix"], False)
        self.assertEqual(self.blend.objects.new_calls, 0)
        self.assertEqual(self.blend.meshes.new_calls, 0)
        self.assertNotIn("Cube.001", self.object_names_now())

    def test_target_collection_guard(self):
        linked = self.blend.add_collection("Linked", parents=[self.root], library=object())
        other = self.blend.add_scene("Other")
        shared = self.blend.add_collection("Shared", parents=[self.root, other.collection])
        self.error("collection_not_found", "object_create", scene="Main", path="Ghost", name="X")
        self.error("read_only_collection", "object_create", scene="Main", path="Linked", name="X")
        self.error("collection_shared_across_scenes", "object_create", scene="Main",
                   path="Shared", name="X")
        self.assertEqual(self.blend.objects.new_calls, 0)

    def test_renamed_allocations_are_detected_and_only_our_datablocks_are_removed(self):
        self.blend.force_new_name = "X.001"
        caught = self.error("invalid_name", "object_create", scene="Main", path="", name="X",
                            vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
        self.assertEqual(caught.details["stored_name"], "X.001")
        self.assertEqual(caught.details["cleanup"]["data"], "X.001")
        self.assertEqual(caught.details["cleanup"]["removed"], ["X.001"])
        self.assertTrue(caught.details["cleanup"]["clean"])
        self.assertEqual(self.blend.meshes.remove_calls, 1)
        self.assertEqual(self.blend.objects.new_calls, 0)
        self.assertNotIn("X.001", self.mesh_names())
        self.assertEqual(self.object_names_now(), ["Cam", "Cube"])

    def test_link_failure_cleans_the_fresh_mesh_and_object(self):
        self.blend.link_hook = lambda owner, item, kind: (
            kind == "objects" and getattr(item, "name", None) == "Fresh")
        caught = self.error("outliner_write_failed", "object_create", scene="Main", path="Props",
                            name="Fresh", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                            faces=[[0, 1, 2]])
        cleanup = caught.details["cleanup"]
        self.assertIn("Fresh", cleanup["removed"])
        self.assertIn("Fresh", cleanup["data"])
        self.assertEqual(cleanup["residual"], [])
        self.assertTrue(cleanup["clean"])
        self.assertEqual(self.blend.objects.new_calls, 1)
        self.assertEqual(self.blend.meshes.new_calls, 1)
        self.assertEqual(self.blend.objects.remove_calls, 1)
        self.assertEqual(self.blend.meshes.remove_calls, 1)
        self.assertEqual(self.object_names_now(), ["Cam", "Cube"])
        self.assertEqual(self.mesh_names(), ["Cube_mesh"])
        self.assertEqual(list(self.props.children), [])

    def test_failed_geometry_creation_leaves_no_mesh_behind(self):
        def from_pydata_hook(mesh):
            raise RuntimeError("injected from_pydata failure")

        original = LiveMesh.from_pydata
        LiveMesh.from_pydata = lambda self, *args, **kwargs: from_pydata_hook(self)
        try:
            caught = self.error("outliner_write_failed", "object_create", scene="Main", path="",
                                name="Broken", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                                faces=[[0, 1, 2]])
        finally:
            LiveMesh.from_pydata = original
        self.assertIn("from_pydata", caught.details["error"])
        self.assertEqual(caught.details["cleanup"]["removed"], ["Broken"])
        self.assertTrue(caught.details["cleanup"]["clean"])
        self.assertEqual(self.blend.meshes.remove_calls, 1)
        self.assertEqual(self.object_names_now(), ["Cam", "Cube"])

    def test_creation_never_modifies_existing_data(self):
        cube_data = self.cube.data
        before = (self.cube.snapshot(), [group.name for group in self.blend.node_groups],
                  self.mesh_names())
        result = self.call("object_create", scene="Main", path="Props", name="Fresh",
                           vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
        self.assertEqual(result["objects_deleted"], [])
        self.assertIs(self.cube.data, cube_data)
        self.assertEqual(self.cube.snapshot(), before[0])
        self.assertEqual(self.cube.data.name, "Cube_mesh")
        self.assertEqual([group.name for group in self.blend.node_groups], before[1])
        # only the new mesh was added; every pre-existing mesh is untouched
        self.assertEqual(self.mesh_names(), sorted(before[2] + ["Fresh"]))

    def test_payload_is_json_serializable(self):
        payload = self.call("object_create", scene="Main", path="", name="JsonMesh",
                            vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
        self.assertIsInstance(json.loads(json.dumps(payload)), dict)


# --------------------------------------------------------------------------- #
# object rename
# --------------------------------------------------------------------------- #
class ObjectRenameTests(ManagementTest):
    def test_rename_keeps_data_modifiers_groups_drivers_and_shape_keys(self):
        self.cube.animation_data = types.SimpleNamespace(drivers=[types.SimpleNamespace(name="d1")])
        self.cube.data.shape_keys = types.SimpleNamespace(
            key_blocks=[types.SimpleNamespace(name="Basis")])
        self.cube.vertex_groups = [types.SimpleNamespace(name="Group") ]
        before = self.cube.snapshot()
        result = self.call("object_rename", scene="Main", object="Cube", name="Renamed")
        self.assertEqual(result["previous_name"], "Cube")
        self.assertEqual(result["object"], "Renamed")
        self.assertTrue(result["references_preserved"])
        self.assertEqual(result["references"]["data"], "Cube_mesh")
        self.assertEqual(result["references"]["node_groups"], ["GN_A"])
        self.assertEqual(result["references"]["driver_count"], 1)
        self.assertEqual(result["references"]["shape_keys"], ["Basis"])
        self.assertEqual(result["references"]["modifier_count"], 1)
        self.assertIs(self.blend.object("Renamed"), self.cube)
        self.assertEqual(self.cube.data.name, "Cube_mesh")
        self.assertEqual(self.cube.snapshot(), before)
        self.assertEqual([collection.name for collection in self.cube.users_collection],
                         ["Main", "Props"])
        self.assertEqual(self.cube.modifiers[0].node_group, self.group_a)
        self.assertEqual(self.cube.data.shape_keys.key_blocks[0].name, "Basis")

    def test_rename_is_idempotent_and_conflicts_are_refused_without_suffix(self):
        repeat = self.call("object_rename", scene="Main", object="Cube", name="Cube")
        self.assertFalse(repeat["changed"])
        self.assertTrue(repeat["idempotent"])
        caught = self.error("object_name_conflict", "object_rename", scene="Main",
                            object="Cube", name="Cam")
        self.assertIs(caught.details["auto_suffix"], False)
        self.assertEqual(self.cube.name, "Cube")
        self.assertNotIn("Cam.001", self.object_names_now())

    def test_guards_and_lookup_errors(self):
        self.error("object_not_found", "object_rename", scene="Main", object="Ghost", name="X")
        loose = self.blend.add_object("Loose", collections=[])
        self.error("object_not_in_scene", "object_rename", scene="Main", object="Loose", name="X")
        other = self.blend.add_scene("Other")
        shared = self.blend.add_object("SharedCube", collections=[self.root, other.collection])
        self.error("object_shared_across_scenes", "object_rename", scene="Main",
                   object="SharedCube", name="X")
        linked = self.blend.add_object("LinkedObj", collections=[self.root], library=object())
        self.error("read_only_object", "object_rename", scene="Main", object="LinkedObj", name="X")
        self.assertEqual(loose.name, "Loose")
        self.assertEqual(shared.name, "SharedCube")
        self.assertEqual(linked.name, "LinkedObj")
        self.assertFalse(any(".001" in name for name in self.object_names_now()))

    def test_failure_and_drift_restore_the_previous_name(self):
        self.blend.rename_hook = lambda block, value: getattr(block, "name", None) == "Cube"
        self.error("outliner_write_failed", "object_rename", scene="Main", object="Cube", name="Renamed")
        self.assertEqual(self.cube.name, "Cube")
        self.blend.rename_hook = None
        self.blend.force_stored_name = "Renamed.001"
        caught = self.error("invalid_name", "object_rename", scene="Main", object="Cube", name="Renamed")
        self.assertEqual(caught.details["stored_name"], "Renamed.001")
        self.assertEqual(caught.details["restored_name"], "Cube")
        self.assertEqual(self.cube.name, "Cube")
        self.assertEqual(caught.details["references"]["data"], "Cube_mesh")

    def test_reference_change_restores_the_old_name(self):
        self.cube.constraints = [types.SimpleNamespace(name="Keep")]
        self.blend.rename_drops_reference = True
        caught = self.error("outliner_write_failed", "object_rename", scene="Main",
                            object="Cube", name="Renamed")
        self.assertIn("changed data-block references", caught.message)
        self.assertEqual(caught.details["before"]["constraints"], ["Keep"])
        self.assertEqual(caught.details["after"]["constraints"], [])
        self.assertEqual(caught.details["restored_name"], "Cube")
        self.assertEqual(self.cube.name, "Cube")

    def test_rename_payload_is_json_serializable(self):
        payload = self.call("object_rename", scene="Main", object="Cube", name="Renamed")
        self.assertIsInstance(json.loads(json.dumps(payload)), dict)


# --------------------------------------------------------------------------- #
# object delete
# --------------------------------------------------------------------------- #
class ObjectDeleteTests(ManagementTest):
    def mapped_scene_user(self, scene):
        original = self.blend.user_map

        def mapping(subset=None):
            result = original(subset=subset)
            for users in result.values():
                users.add(scene)
            return result

        self.blend.user_map = mapping

    def test_owning_scene_structural_user_does_not_block_delete(self):
        self.mapped_scene_user(self.scene)
        mesh = self.cube.data
        result = self.call("object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(result["objects_deleted"], ["Cube"])
        self.assertIn(mesh, list(self.blend.meshes))

    def test_direct_and_weak_scene_references_still_block_delete(self):
        self.mapped_scene_user(self.scene)
        self.scene.camera = self.cube
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.scene.camera = None
        self.scene.timeline_markers = [types.SimpleNamespace(camera=self.cube)]
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.scene.timeline_markers = []
        variable = types.SimpleNamespace(targets=[types.SimpleNamespace(id=self.cube)])
        self.scene.animation_data = types.SimpleNamespace(drivers=[types.SimpleNamespace(
            driver=types.SimpleNamespace(variables=[variable]))])
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.scene.animation_data = None
        self.scene.keying_sets = [types.SimpleNamespace(paths=[types.SimpleNamespace(id=self.cube)])]
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_nonowning_scene_reference_is_never_structural(self):
        other = self.blend.add_scene("Other")
        self.mapped_scene_user(other)
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_extra_users_keep_owning_scene_reference_protected(self):
        self.mapped_scene_user(self.scene)
        self.blend.extra_users[id(self.cube)] = 1
        self.error("object_in_use", "object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_delete_checks_scenes_beyond_compact_membership_report(self):
        for number in range(35):
            owner = self.blend.add_collection("Owner%02d" % number, parents=[self.root])
            owner.objects.link(self.cube)
        other = self.blend.add_scene("Other")
        foreign = self.blend.add_collection("ZZForeign", parents=[other.collection])
        foreign.objects.link(self.cube)
        self.error("object_shared_across_scenes", "object_delete", scene="Main",
                   objects=["Cube"], confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_confirmation_is_required(self):
        caught = self.error("confirmation_required", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=False)
        self.assertEqual(caught.details["objects"], ["Cube"])
        self.assertIs(caught.details["confirm_delete"], False)
        self.error("invalid_argument", "object_delete", scene="Main", objects=["Cube"],
                   confirm_delete="yes")
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_deletes_only_the_listed_objects_and_preserves_data(self):
        group = self.blend.add_node_group("GN_Keep")
        cube_mesh = self.cube.data
        meshes_before = self.mesh_names()
        result = self.call("object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(result["objects_deleted"], ["Cube"])
        self.assertEqual(result["deleted"], ["Cube"])
        self.assertIs(result["partial"], False)
        self.assertEqual(result["data_blocks_deleted"], [])
        self.assertEqual(result["data_preserved"][0]["data"], "Cube_mesh")
        self.assertIs(result["data_preserved"][0]["preserved"], True)
        self.assertEqual(self.blend.objects.remove_calls, 1)
        self.assertEqual(self.blend.meshes.remove_calls, 0)
        self.assertEqual(self.mesh_names(), meshes_before)
        self.assertEqual(cube_mesh.name, "Cube_mesh")
        self.assertEqual([item.name for item in self.blend.node_groups], ["GN_A", "GN_B", "GN_Keep"])
        self.assertIs(self.blend.object("Cam"), self.camera)
        self.assertTrue(any("not purged" in warning for warning in result["warnings"]))
        with self.assertRaises(ReferenceError):
            self.cube.name  # noqa: B018 - only cached names may be reported afterwards

    def test_one_bad_name_deletes_nothing(self):
        caught = self.error("object_not_found", "object_delete", scene="Main",
                            objects=["Cube", "Ghost"], confirm_delete=True)
        self.assertIn("available", caught.details)
        self.error("duplicate_object", "object_delete", scene="Main", objects=["Cube", "Cube"],
                   confirm_delete=True)
        self.error("invalid_objects", "object_delete", scene="Main", objects=[],
                   confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)
        self.assertEqual(self.object_names_now(), ["Cam", "Cube"])

    def test_a_rejected_batch_deletes_nothing(self):
        other = self.blend.add_scene("Other")
        shared = self.blend.add_object("SharedCube", collections=[self.root, other.collection])
        caught = self.error("object_shared_across_scenes", "object_delete", scene="Main",
                            objects=["Cube", "SharedCube"], confirm_delete=True)
        self.assertEqual(caught.details["problem_count"], 1)
        self.assertEqual(self.blend.objects.remove_calls, 0)
        self.assertIs(self.blend.object("Cube"), self.cube)
        self.assertIs(self.blend.object("SharedCube"), shared)

    def test_linked_and_multi_scene_objects_are_refused(self):
        other = self.blend.add_scene("Other")
        shared = self.blend.add_object("SharedCube", collections=[self.root, other.collection])
        linked = self.blend.add_object("LinkedObj", collections=[self.root], library=object())
        override = self.blend.add_object("OverrideObj", collections=[self.root],
                                        override_library=object())
        loose = self.blend.add_object("Loose", collections=[])
        self.error("object_shared_across_scenes", "object_delete", scene="Main",
                   objects=["SharedCube"], confirm_delete=True)
        self.error("read_only_object", "object_delete", scene="Main",
                   objects=["LinkedObj"], confirm_delete=True)
        self.error("read_only_object", "object_delete", scene="Main",
                   objects=["OverrideObj"], confirm_delete=True)
        self.error("object_not_in_scene", "object_delete", scene="Main",
                   objects=["Loose"], confirm_delete=True)
        self.assertEqual(self.blend.objects.remove_calls, 0)
        self.assertIs(self.blend.object("Loose"), loose)
        self.assertIs(self.blend.object("SharedCube"), shared)

    def test_referenced_objects_are_protected(self):
        # child object
        child = self.blend.add_object("Child", collections=[self.root], parent=self.cube)
        caught = self.error("object_in_use", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "child_object")
        self.assertEqual(caught.details["problems"][0]["users"][0]["user"], "Child")
        child.parent = None
        # constraint target
        self.camera.constraints = [types.SimpleNamespace(name="Copy", target=self.cube, object=None)]
        caught = self.error("object_in_use", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "constraint_target")
        self.camera.constraints = []
        # modifier object
        self.camera.modifiers = [types.SimpleNamespace(type="BOOLEAN", node_group=None,
                                                       object=self.cube)]
        caught = self.error("object_in_use", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "modifier_object")
        self.camera.modifiers = []
        # geometry nodes object socket reference
        group = self.blend.add_node_group("GN_Object", object=self.cube)
        caught = self.error("object_in_use", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=True)
        self.assertEqual(caught.details["problems"][0]["reason"], "geometry_nodes_object_reference")
        self.assertEqual(caught.details["problems"][0]["users"][0]["user"], "GN_Object")
        # the simulated reference also keeps the object used, so the refusal is stable
        self.assertEqual(self.cube.users > len(self.cube.users_collection), True)
        self.assertIs(self.blend.object("Cube"), self.cube)
        self.assertEqual(self.blend.objects.remove_calls, 0)
        self.assertIn(group.name, [item.name for item in self.blend.node_groups])
        # removing the reference makes the deletion possible again
        group.nodes = []
        result = self.call("object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertEqual(result["objects_deleted"], ["Cube"])

    def test_unprovable_users_refuse_the_delete(self):
        self.blend.extra_users[id(self.cube)] = 2
        caught = self.error("object_in_use", "object_delete", scene="Main",
                            objects=["Cube"], confirm_delete=True)
        problem = caught.details["problems"][0]
        self.assertEqual(problem["reason"], "users_count")
        self.assertEqual(problem["external_user_count"], 2)
        self.assertIn("never deleted", caught.message)
        self.assertEqual(self.blend.objects.remove_calls, 0)

    def test_partial_removal_is_reported_honestly(self):
        second = self.blend.add_object("Second", collections=[self.root])
        calls = {"n": 0}

        def object_remove_hook(obj):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected object remove failure")

        self.blend.object_remove_hook = object_remove_hook
        caught = self.error("outliner_delete_partial", "object_delete", scene="Main",
                            objects=["Cube", "Second"], confirm_delete=True)
        self.assertEqual(caught.details["deleted"], ["Cube"])
        self.assertEqual(caught.details["residual"], ["Second"])
        self.assertIs(caught.details["partial"], True)
        self.assertIs(caught.details["not_transactional"], True)
        self.assertIs(caught.details["rolled_back"], False)
        self.assertIn("injected object remove failure", caught.details["errors"][0]["error"])
        self.assertIsNone(self.blend.object("Second"))
        self.assertEqual(caught.details["data_blocks_deleted"], [])

    def test_delete_payload_is_json_serializable(self):
        payload = self.call("object_delete", scene="Main", objects=["Cube"], confirm_delete=True)
        self.assertIsInstance(json.loads(json.dumps(payload)), dict)
        self.assertEqual(payload["data_preserved"][0]["data"], "Cube_mesh")


# --------------------------------------------------------------------------- #
# MCP tool surface
# --------------------------------------------------------------------------- #
class McpToolTests(unittest.TestCase):
    EXPECTED = {
        "blender_outliner_collection_create": "outliner_collection_create",
        "blender_outliner_collection_rename": "outliner_collection_rename",
        "blender_outliner_collection_move": "outliner_collection_move",
        "blender_outliner_collection_delete": "outliner_collection_delete",
        "blender_outliner_object_create": "outliner_object_create",
        "blender_outliner_object_rename": "outliner_object_rename",
        "blender_outliner_object_delete": "outliner_object_delete",
        "blender_outliner_collection_display": "outliner_collection_display",
        "blender_outliner_object_display": "outliner_object_display",
    }
    AUDIT_DIR = RUNTIME / "outliner_management_test_audit"

    @classmethod
    def setUpClass(cls):
        from blender_mcp import audit
        from blender_mcp.tools import AuditedFastMCP
        from blender_mcp.tools import outliner_management as tools

        audit.configure(directory=cls.AUDIT_DIR, source="test")
        cls.server = AuditedFastMCP(name="outliner-management-tests")
        cls.bridge = StubBridge()
        tools.register_outliner_management_tools(cls.server, cls.bridge)
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.server.list_tools())}

    def test_register_alias_and_exact_tool_names(self):
        from blender_mcp.tools import outliner_management as tools

        self.assertIs(tools.register, tools.register_outliner_management_tools)
        self.assertEqual(sorted(self.tools), sorted(self.EXPECTED))
        self.assertEqual(tools.TOOL_COMMANDS, self.EXPECTED)
        for removed in REMOVED_INSTANCE_COMMANDS:
            self.assertNotIn(removed, self.tools)
        obj_create = self.tools["blender_outliner_object_create"].inputSchema
        self.assertEqual(sorted(obj_create["properties"]),
                         ["faces", "name", "path", "scene", "type", "vertices"])

    @staticmethod
    def _array_schema(prop):
        """The array branch of a schema property (nullable vectors use ``anyOf``)."""
        if "maxItems" in prop or "minItems" in prop:
            return prop
        for option in prop.get("anyOf", []):
            if option.get("type") == "array":
                return option
        return prop

    def test_locked_object_create_schema(self):
        schema = self.tools["blender_outliner_object_create"].inputSchema
        self.assertEqual(schema["required"], ["scene", "path", "name"])
        self.assertEqual(schema["properties"]["type"]["default"], "MESH")
        self.assertEqual(sorted(schema["properties"]["type"]["enum"]), ["EMPTY", "MESH"])
        vectors = self._array_schema(schema["properties"]["vertices"])
        self.assertIsNone(schema["properties"]["vertices"]["default"])
        self.assertEqual(vectors["maxItems"], 20000)
        self.assertEqual(vectors["items"]["minItems"], 3)
        self.assertEqual(vectors["items"]["maxItems"], 3)
        self.assertEqual(vectors["items"]["items"]["type"], "number")
        faces = self._array_schema(schema["properties"]["faces"])
        self.assertIsNone(schema["properties"]["faces"]["default"])
        self.assertEqual(faces["maxItems"], 20000)
        self.assertEqual(faces["items"]["minItems"], 3)
        self.assertEqual(faces["items"]["items"]["type"], "integer")
        self.assertEqual(schema["properties"]["name"]["maxLength"], 255)
        self.assertEqual(schema["properties"]["path"]["maxLength"], 1024)

    def test_other_schemas_are_typed(self):
        delete = self.tools["blender_outliner_collection_delete"].inputSchema
        self.assertEqual(delete["required"], ["scene", "path", "confirm_delete"])
        self.assertEqual(delete["properties"]["recursive"]["default"], False)
        objects = self.tools["blender_outliner_object_delete"].inputSchema
        self.assertEqual(objects["required"], ["scene", "objects", "confirm_delete"])
        self.assertEqual(objects["properties"]["objects"]["minItems"], 1)
        self.assertEqual(objects["properties"]["objects"]["maxItems"], 512)
        self.assertNotIn("permission", objects["properties"])
        self.assertNotIn("force", objects["properties"])
        rename = self.tools["blender_outliner_object_rename"].inputSchema
        self.assertEqual(rename["required"], ["scene", "object", "name"])
        for name in ("blender_outliner_collection_display", "blender_outliner_object_display"):
            display = self.tools[name].inputSchema
            self.assertIn("view_layer", display["required"])
            for flag in ("hidden", "disable_viewport", "hide_render"):
                self.assertIsNone(display["properties"][flag]["default"], name)
        collection_display = self.tools["blender_outliner_collection_display"].inputSchema
        self.assertIn("exclude", collection_display["properties"])
        self.assertNotIn("exclude", self.tools["blender_outliner_object_display"].inputSchema["properties"])

    def test_title_annotations_and_domain_meta(self):
        expectations = {
            "blender_outliner_collection_create": (False, False, True, False),
            "blender_outliner_collection_rename": (False, True, True, False),
            "blender_outliner_collection_move": (False, True, True, False),
            "blender_outliner_collection_delete": (False, True, False, True),
            "blender_outliner_object_create": (False, False, False, False),
            "blender_outliner_object_rename": (False, True, True, False),
            "blender_outliner_object_delete": (False, True, False, True),
            "blender_outliner_collection_display": (False, False, True, False),
            "blender_outliner_object_display": (False, False, True, False),
        }
        for name, (read_only, destructive, idempotent, deletes) in expectations.items():
            tool = self.tools[name]
            self.assertTrue(tool.title, name)
            self.assertEqual(tool.annotations.readOnlyHint, read_only, name)
            self.assertEqual(tool.annotations.destructiveHint, destructive, name)
            self.assertEqual(tool.annotations.idempotentHint, idempotent, name)
            self.assertEqual(tool.annotations.openWorldHint, False, name)
            self.assertEqual(tool.meta["domain"], "outliner", name)
            self.assertEqual(tool.meta["bridge_prefix"], "outliner_", name)
            self.assertIs(tool.meta["deletes_data"], deletes, name)
            self.assertEqual(tool.meta["mutating"], not read_only, name)
            self.assertEqual(tool.meta["object_kind"], "ordinary_mesh_empty", name)
            self.assertEqual(tool.name.replace("blender_", ""), self.EXPECTED[name], name)
        for name in ("blender_outliner_collection_display", "blender_outliner_object_display"):
            meta = self.tools[name].meta
            self.assertIs(meta["per_view_layer"], True, name)
            self.assertIs(meta["monitor_property_written"], True, name)
            self.assertEqual(meta["datablock_flags"], ["disable_viewport", "hide_render"], name)
        self.assertEqual(self.tools["blender_outliner_collection_display"].meta["per_view_layer_flags"],
                         ["hidden", "exclude"])
        self.assertEqual(self.tools["blender_outliner_object_display"].meta["per_view_layer_flags"],
                         ["hidden"])

    def test_forwarding_is_typed_for_all_nine_tools(self):
        cases = (
            ("blender_outliner_collection_create", {"scene": "Main", "parent_path": "A", "name": "B"},
             {"scene": "Main", "parent_path": "A", "name": "B"}),
            ("blender_outliner_collection_rename", {"scene": "Main", "path": "A", "name": "B"},
             {"scene": "Main", "path": "A", "name": "B"}),
            ("blender_outliner_collection_move", {"scene": "Main", "path": "A", "parent_path": ""},
             {"scene": "Main", "path": "A", "parent_path": ""}),
            ("blender_outliner_collection_delete",
             {"scene": "Main", "path": "A", "confirm_delete": True, "recursive": True},
             {"scene": "Main", "path": "A", "confirm_delete": True, "recursive": True}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "A", "name": "O", "vertices": [[0.0, 0.0, 0.0]],
              "faces": [[0, 0, 0]]},
             {"scene": "Main", "path": "A", "name": "O", "type": "MESH",
              "vertices": [[0.0, 0.0, 0.0]], "faces": [[0, 0, 0]]}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "E", "type": "EMPTY"},
             {"scene": "Main", "path": "", "name": "E", "type": "EMPTY",
              "vertices": None, "faces": None}),
            # The exact payload the live acceptance test sends: JSON integers must be
            # coerced to the float triples the bridge validates.
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "Live", "type": "MESH",
              "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]},
             {"scene": "Main", "path": "", "name": "Live", "type": "MESH",
              "vertices": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
              "faces": [[0, 1, 2]]}),
            ("blender_outliner_object_rename", {"scene": "Main", "object": "O", "name": "P"},
             {"scene": "Main", "object": "O", "name": "P"}),
            ("blender_outliner_object_delete",
             {"scene": "Main", "objects": ["O", "P"], "confirm_delete": True},
             {"scene": "Main", "objects": ["O", "P"], "confirm_delete": True}),
            ("blender_outliner_collection_display",
             {"scene": "Main", "path": "A", "view_layer": "ViewLayer", "disable_viewport": True},
             {"scene": "Main", "path": "A", "view_layer": "ViewLayer", "hidden": None,
              "disable_viewport": True, "hide_render": None, "exclude": None}),
            ("blender_outliner_object_display",
             {"scene": "Main", "objects": ["A", "B"], "view_layer": "ViewLayer",
              "hide_render": True},
             {"scene": "Main", "objects": ["A", "B"], "view_layer": "ViewLayer", "hidden": None,
              "disable_viewport": None, "hide_render": True}),
        )
        for tool_name, arguments, expected in cases:
            self.bridge.calls.clear()
            payload = _structured(asyncio.run(self.server.call_tool(tool_name, arguments)))
            self.assertIs(payload.get("ok"), True, tool_name)
            command = self.EXPECTED[tool_name]
            self.assertEqual(self.bridge.commands()[-1], command, tool_name)
            self.assertEqual(self.bridge.params_of(command), expected, tool_name)

    def test_schema_rejects_wrong_types_before_the_bridge(self):
        cases = (
            ("blender_outliner_collection_create", {"scene": "Main", "parent_path": "", "name": ""}),
            ("blender_outliner_collection_delete",
             {"scene": "Main", "path": "A", "confirm_delete": "yes"}),
            ("blender_outliner_collection_delete", {"scene": "Main", "path": "A"}),
            ("blender_outliner_collection_delete",
             {"scene": "Main", "path": "A", "confirm_delete": True, "recursive": 1}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "O", "type": "CURVE"}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "O", "vertices": [[0.0, 0.0]]}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "O", "vertices": [[0.0, 0.0, 0.0]],
              "faces": [[0, 1]]}),
            ("blender_outliner_object_create",
             {"scene": "Main", "path": "", "name": "O", "faces": [[0, 1, 2.5]]}),
            ("blender_outliner_object_delete", {"scene": "Main", "objects": [], "confirm_delete": True}),
            ("blender_outliner_object_delete", {"scene": "Main", "objects": "O", "confirm_delete": True}),
            ("blender_outliner_object_delete", {"scene": "Main", "objects": ["O"]}),
            ("blender_outliner_object_delete",
             {"scene": "Main", "objects": ["O"], "confirm_delete": "true"}),
            ("blender_outliner_object_rename", {"scene": "Main", "object": "O"}),
            ("blender_outliner_object_display",
             {"scene": "Main", "objects": ["O"], "view_layer": "V", "disable_viewport": 1}),
            ("blender_outliner_collection_display",
             {"scene": "Main", "path": "A", "view_layer": "V", "exclude": "yes"}),
        )
        for tool_name, arguments in cases:
            before = len(self.bridge.calls)
            with self.assertRaises(Exception, msg="%s %s" % (tool_name, arguments)):
                asyncio.run(self.server.call_tool(tool_name, arguments))
            self.assertEqual(len(self.bridge.calls), before, tool_name)

    def test_calls_are_audited_by_the_decorated_server(self):
        from blender_mcp import audit

        name = "blender_outliner_object_create"
        asyncio.run(self.server.call_tool(name, {"scene": "Main", "path": "", "name": "Audited"}))
        events = audit.tail_events(limit=200, directory=self.AUDIT_DIR)
        starts = [event for event in events if event.get("event") == "tool_start" and event.get("tool") == name]
        ends = [event for event in events if event.get("event") == "tool_end" and event.get("tool") == name]
        self.assertTrue(starts, "the audited server must record tool_start")
        self.assertTrue(ends, "the audited server must record tool_end")
        self.assertEqual(ends[-1]["ok"], True)
        self.assertEqual(ends[-1]["status"], "ok")

    def test_registration_through_the_domain_proxy_exposes_all_nine(self):
        from blender_mcp.tools import AuditedFastMCP
        from blender_mcp.tools import outliner_management as tools
        from blender_mcp.tools.domains import _DomainRegistrar

        server = AuditedFastMCP(name="outliner-management-domain-proxy")
        names = set()
        registrar = _DomainRegistrar(server, "outliner", names)
        tools.register(registrar, StubBridge())
        exposed = sorted(tool.name for tool in asyncio.run(server.list_tools()))
        self.assertEqual(exposed, sorted(self.EXPECTED))
        self.assertEqual(names, set(self.EXPECTED))

    def test_tools_module_never_imports_bpy(self):
        from blender_mcp.tools import outliner_management as tools

        self.assertNotIn("bpy", tools.__dict__)
        source = TOOLS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)
        self.assertNotIn("from bpy", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
