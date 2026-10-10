"""Properties-domain regressions: a pure in-memory fake ``bpy``, no Blender needed.

Two contracts are covered:

* the bridge module ``blender/properties.py`` - per-instance Geometry Nodes modifier
  input enumeration, strict typed writes, the read-only guards (linked/override,
  driver, hidden, geometry/bundle, unproven attribute/menu representations), batch
  pre-flight with zero side effects, rollback of value *and* mode keys, read-back
  verification, and the honest dependency-graph/evaluation report;
* the MCP module ``blender_mcp/tools/properties.py`` - the four typed
  ``blender_properties_*`` schemas (title / annotations / domain ``meta``), the exact
  parameters forwarded to the bridge, and that registering this domain changes no
  existing tool schema.

Nothing here touches ``Project`` or a real ``.blend``: the fake models
``bpy.data`` (objects, node groups, ID collections), ``NodeTree`` interfaces with
``identifier``/``socket_type``/range/hidden flags, the ``modifier.properties.inputs``
IDPropertyGroup (including attribute-name and menu representations), object/view-layer
scenes and the dependency graph, and it can inject write failures, silent value
corruption and evaluation errors.

The fake ``api`` mirrors the add-on helper surface my module is allowed to use; a
static test below asserts that ``blender/blender_mcp_addon.py`` really defines every
name it relies on, so the seam cannot rot silently.  What this suite does **not**
prove is Blender 5.2.1 behaviour: real-Blinker verification is listed as pending in
``docs/PROPERTIES.md``.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / "blender" / "properties.py"
TOOLS_PATH = ROOT / "blender_mcp" / "tools" / "properties.py"
ADDON_PATH = ROOT / "blender" / "blender_mcp_addon.py"
RUNTIME = ROOT / ".runtime"
MISSING = object()

COMMAND_NAMES = (
    "properties_get_parameter",
    "properties_list_parameters",
    "properties_set_parameter",
    "properties_set_parameters",
)
TOOL_NAMES = tuple("blender_" + name for name in COMMAND_NAMES)


class CommandFailure(Exception):
    """The bridge ``CommandError`` shape (code / message / details)."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}


# --------------------------------------------------------------------------- #
# fake Blender data model
# --------------------------------------------------------------------------- #
class Registry(list):
    """A ``bpy.data`` collection: iterable plus ``get(name)``."""

    def get(self, name, default=None):
        for item in self:
            if getattr(item, "name", None) == name:
                return item
        return default

    def names(self):
        return [item.name for item in self]


class InputGroup:
    """One socket's IDPropertyGroup: ``{"value": ..., "attribute_name": ...}``."""

    def __init__(self, blend, identifier, data=None):
        self._blend = blend
        self.identifier = identifier
        self._data = copy.deepcopy(dict(data or {}))
        if "type" in self._data:
            self.bl_rna = types.SimpleNamespace(properties={
                "type": types.SimpleNamespace(type="ENUM", enum_items=[
                    types.SimpleNamespace(identifier="VALUE", value=1),
                    types.SimpleNamespace(identifier="ATTRIBUTE", value=2)])})

    def keys(self):
        return list(self._data)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        hook = self._blend.write_hook
        if hook is not None and hook(self.identifier, key, value):
            raise RuntimeError("injected write failure for %s.%s" % (self.identifier, key))
        corrupt = self._blend.corrupt_hook
        if corrupt is not None:
            value = corrupt(self.identifier, key, value)
        self._data[key] = value

    def __contains__(self, key):
        return key in self._data

    def to_dict(self):
        return dict(self._data)


class Inputs:
    """``modifier.properties.inputs``: keyed by interface socket identifier."""

    def __init__(self, blend, data=None):
        self._blend = blend
        self._data = {str(key): InputGroup(blend, str(key), value)
                      for key, value in dict(data or {}).items()}

    def keys(self):
        return list(self._data)

    def __getitem__(self, identifier):
        return self._data[identifier]

    def __contains__(self, identifier):
        return identifier in self._data

    def __getattr__(self, identifier):
        if identifier in self._data:
            return self._data[identifier]
        raise AttributeError(identifier)

    def to_dict(self):
        return {key: group.to_dict() for key, group in self._data.items()}


class Properties:
    def __init__(self, inputs):
        self.inputs = inputs


class Modifier:
    def __init__(self, blend, name, type="NODES", node_group=None, inputs=None,
                 with_properties=True, library=None, override_library=None,
                 is_library_indirect=False):
        self._blend = blend
        self.name = name
        self.type = type
        self.node_group = node_group
        self.library = library
        self.override_library = override_library
        self.is_library_indirect = is_library_indirect
        self.show_viewport = True
        self.show_render = True
        if with_properties:
            self.properties = Properties(Inputs(blend, inputs))
        else:
            self.properties = None


class ModifierList(list):
    def get(self, name, default=None):
        for item in self:
            if item.name == name:
                return item
        return default


class DriverCurve:
    def __init__(self, data_path, expression="1.0", array_index=0, is_valid=True):
        self.data_path = data_path
        self.array_index = array_index
        self.driver = types.SimpleNamespace(expression=expression, is_valid=is_valid)


class AnimationData:
    def __init__(self, drivers=()):
        self.drivers = list(drivers)


class Block:
    """An ID datablock with an RNA type name."""

    def __init__(self, name, rna):
        self.name = name
        self.bl_rna = types.SimpleNamespace(identifier=rna)
        self.library = None
        self.override_library = None
        self.is_library_indirect = False

    def as_pointer(self):
        return id(self)


class Object(Block):
    def __init__(self, name):
        super().__init__(name, "Object")
        self.type = "MESH"
        self.modifiers = ModifierList()
        self.animation_data = None
        self.update_tag_calls = 0
        self.base_vertices = 8
        self.evaluated_vertices = 8

    def update_tag(self):
        self.update_tag_calls += 1


class NodeGroup(Block):
    def __init__(self, name, items=(), nodes=()):
        super().__init__(name, "GeometryNodeTree")
        self.interface = types.SimpleNamespace(items_tree=list(items))
        self.nodes = list(nodes)
        self.update_tag_calls = 0

    def update_tag(self):
        self.update_tag_calls += 1


class InterfaceItem:
    """One ``NodeTreeInterfaceSocket`` (or panel)."""

    def __init__(self, name, identifier, socket_type=None, in_out="INPUT", default_value=None,
                 min_value=None, max_value=None, subtype=None, description="",
                 hide_value=False, hide_in_modifier=False, default_attribute_name=None,
                 enum_items=None, enum_rna_items=None, item_type="SOCKET", pointer_rna=None):
        self.name = name
        self.identifier = identifier
        self.socket_type = socket_type
        self.in_out = in_out
        self.item_type = item_type
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value
        self.subtype = subtype
        self.description = description
        self.hide_value = hide_value
        self.hide_in_modifier = hide_in_modifier
        self.default_attribute_name = default_attribute_name
        if enum_items is not None:
            self.enum_items = [types.SimpleNamespace(identifier=identifier_, name=name_, value=value)
                               for value, (identifier_, name_) in enumerate(enum_items)]
        if pointer_rna is not None:
            self.bl_rna = types.SimpleNamespace(
                identifier="NodeTreeInterfaceSocket",
                properties={"default_value": types.SimpleNamespace(
                    type="POINTER", fixed_type=types.SimpleNamespace(identifier=pointer_rna))},
            )
        else:
            # ``enum_rna_items`` models the dynamic enum RNA property of a menu socket.
            properties = {"default_value": types.SimpleNamespace(
                type="ENUM" if enum_rna_items else "FLOAT",
                enum_items=[types.SimpleNamespace(identifier=identifier_, name=name_, value=value)
                            for value, (identifier_, name_) in enumerate(enum_rna_items or [])],
            )}
            self.bl_rna = types.SimpleNamespace(
                identifier="NodeTreeInterfaceSocket", properties=properties
            )


class Depsgraph:
    def __init__(self, blend, name="depsgraph"):
        self._blend = blend
        self.name = name
        self.update_calls = 0

    def update(self):
        self.update_calls += 1
        self._blend.depsgraph_updates += 1
        if self._blend.depsgraph_error:
            raise RuntimeError(self._blend.depsgraph_error)
        return self


class ViewLayer:
    def __init__(self, name):
        self.name = name
        self.objects = Registry()
        self.depsgraph = None
        self.update_calls = 0

    def update(self):
        self.update_calls += 1


class Scene:
    def __init__(self, name):
        self.name = name
        self.frame_current = 1
        self.view_layers = [ViewLayer("ViewLayer")]
        self.objects = Registry()
        self.update_tag_calls = 0

    def update_tag(self):
        self.update_tag_calls += 1


class FakeBlend:
    """The fake ``bpy.data`` plus fixture builders and failure injection."""

    def __init__(self):
        self.objects = Registry()
        self.node_groups = Registry()
        self.materials = Registry()
        self.collections = Registry()
        self.images = Registry()
        self.scenes = []
        self.write_hook = None
        self.corrupt_hook = None
        self.depsgraph_error = None
        self.signature_error = None
        self.stats_error = None
        self.depsgraph_updates = 0
        self.stats_calls = []
        self.signature_calls = []

    # -- builders ------------------------------------------------------- #
    def add_scene(self, name):
        scene = Scene(name)
        self.scenes.append(scene)
        return scene

    def add_group(self, name, items, nodes=(), library=None, override_library=None):
        group = NodeGroup(name, items, nodes)
        group.library = library
        group.override_library = override_library
        self.node_groups.append(group)
        return group

    def add_object(self, name, group=None, instance=None, modifier_name="GN",
                   modifier_type="NODES", with_properties=True, library=None,
                   override_library=None, is_library_indirect=False, scene=None):
        obj = Object(name)
        obj.library = library
        obj.override_library = override_library
        obj.is_library_indirect = is_library_indirect
        if group is not None or modifier_type != "NODES":
            obj.modifiers.append(Modifier(self, modifier_name, modifier_type, group, instance,
                                          with_properties, library, override_library,
                                          is_library_indirect))
        self.objects.append(obj)
        target_scene = scene or (self.scenes[0] if self.scenes else None)
        if target_scene is not None:
            target_scene.objects.append(obj)
            target_scene.view_layers[0].objects.append(obj)
            target_scene.view_layers[0].depsgraph = Depsgraph(self)
        return obj


# --------------------------------------------------------------------------- #
# module loading (the stub is only needed while ``blender/properties.py`` imports)
# --------------------------------------------------------------------------- #
_STUB = types.ModuleType("bpy")
_STUB.data = FakeBlend()
_STUB.app = types.SimpleNamespace(version_string="5.2.1")
_PREVIOUS_BPY = sys.modules.get("bpy", MISSING)
sys.modules["bpy"] = _STUB
try:
    _SPEC = importlib.util.spec_from_file_location("properties_under_test", MODULE_PATH)
    MODULE = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(MODULE)
finally:
    if _PREVIOUS_BPY is MISSING:
        sys.modules.pop("bpy", None)
    else:  # pragma: no cover - only when a real bpy is present
        sys.modules["bpy"] = _PREVIOUS_BPY

COMMANDS: dict = {}


def _command(name, mutating=True, summary=""):
    def decorate(func):
        COMMANDS[name] = func
        func.bridge_mutating = mutating
        func.bridge_summary = summary
        return func

    return decorate


# --------------------------------------------------------------------------- #
# faithful stand-ins for the add-on helpers my module is allowed to use
# --------------------------------------------------------------------------- #
REFERENCE_ID_TYPES = {
    "OBJECT": "objects",
    "NODETREE": "node_groups",
    "MATERIAL": "materials",
    "COLLECTION": "collections",
    "IMAGE": "images",
}
REFERENCE_RNA_TYPES = {
    "OBJECT": "Object",
    "NODETREE": "NodeTree",
    "MATERIAL": "Material",
    "COLLECTION": "Collection",
    "IMAGE": "Image",
}


def is_reference_value(value):
    return isinstance(value, dict) and "id_type" in value


def get_object(blend, name):
    obj = blend.objects.get(str(name))
    if obj is None:
        raise CommandFailure("object_not_found", "no object named %r" % name,
                             {"available": blend.objects.names()[:50]})
    return obj


def resolve_id_reference(blend, value):
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise CommandFailure("invalid_reference", "an ID reference must be an object",
                             {"received": value, "supported": sorted(REFERENCE_ID_TYPES)})
    id_type = value.get("id_type")
    if not isinstance(id_type, str) or not id_type.strip():
        raise CommandFailure("invalid_reference", "reference needs a non-empty 'id_type'")
    id_type = id_type.strip().upper()
    if id_type not in REFERENCE_ID_TYPES:
        raise CommandFailure("invalid_reference", "unsupported reference id_type %r" % id_type,
                             {"supported": sorted(REFERENCE_ID_TYPES)})
    id_name = value.get("id_name")
    if id_name is None or (isinstance(id_name, str) and not id_name.strip()):
        return id_type, None
    if not isinstance(id_name, str):
        raise CommandFailure("invalid_reference", "reference 'id_name' must be a string or null")
    collection = getattr(blend, REFERENCE_ID_TYPES[id_type])
    block = collection.get(id_name)
    if block is None:
        raise CommandFailure("reference_not_found", "no %s datablock named %r" % (id_type, id_name),
                             {"id_type": id_type, "id_name": id_name,
                              "available": collection.names()[:50]})
    return id_type, block


def validate_reference_target(prop, id_type, block, label, node=None):
    fixed = getattr(prop, "fixed_type", None)
    expected = getattr(fixed, "identifier", None) if fixed is not None else None
    wanted = REFERENCE_RNA_TYPES[id_type]
    if expected and expected not in (wanted, "ID"):
        raise CommandFailure(
            "reference_type_mismatch",
            "%s expects a %s datablock, not %s (%s)"
            % (label, expected, id_type, getattr(block, "name", None)),
            {"target": label, "expected_rna_type": expected, "given_id_type": id_type},
        )


def local_jsonable(value):
    """A copy of the add-on's private converter, used by the ``with_converters`` api."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, dict):
        return {str(key): local_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [local_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return {str(key): local_jsonable(item) for key, item in value.to_dict().items()}
    return str(value)


def reference_payload(blend, block):
    """A copy of the add-on's ``_id_reference_payload`` (collection fallback included)."""
    if block is None:
        return None
    rna_name = getattr(getattr(block, "bl_rna", None), "identifier", None)
    inverse = {value: key for key, value in REFERENCE_RNA_TYPES.items()}
    id_type = inverse.get(rna_name)
    if id_type is None:
        for candidate, collection_name in REFERENCE_ID_TYPES.items():
            collection = getattr(blend, collection_name, None)
            if collection is not None and collection.get(getattr(block, "name", "")) is block:
                id_type = candidate
                break
    return {"id_type": id_type or rna_name, "id_name": getattr(block, "name", None),
            "rna_type": rna_name}


def bind(blend, with_converters=False):
    """Register the bridge commands against a fresh fake add-on module."""
    COMMANDS.clear()

    def depsgraph_update():
        layer = blend.scenes[0].view_layers[0] if blend.scenes else None
        deps = getattr(layer, "depsgraph", None) or Depsgraph(blend, "context")
        return deps.update()

    def evaluated_mesh_stats(obj, deps=None):
        blend.stats_calls.append({"object": obj.name, "deps": getattr(deps, "name", None)})
        if blend.stats_error:
            raise RuntimeError(blend.stats_error)
        return {"base_vertices": obj.base_vertices,
                "evaluated": {"vertices": obj.evaluated_vertices, "edges": 0, "polygons": 0,
                              "loops": 0, "name": "GN"},
                "bounds": None}

    def mesh_signature(obj, deps=None):
        blend.signature_calls.append({"object": obj.name, "deps": getattr(deps, "name", None)})
        if blend.signature_error:
            raise RuntimeError(blend.signature_error)
        return {"vertices": obj.evaluated_vertices, "checksum": 12345}

    api = types.SimpleNamespace(
        CommandError=CommandFailure,
        command=_command,
        get_object=lambda name: get_object(blend, name),
        flush_object=lambda obj: obj.update_tag(),
        depsgraph_update=depsgraph_update,
        evaluated_mesh_stats=evaluated_mesh_stats,
        mesh_signature=mesh_signature,
        is_reference_value=is_reference_value,
        resolve_id_reference=lambda value: resolve_id_reference(blend, value),
        validate_reference_target=validate_reference_target,
        REFERENCE_ID_TYPES=REFERENCE_ID_TYPES,
        REFERENCE_RNA_TYPES=REFERENCE_RNA_TYPES,
    )
    if with_converters:
        api._jsonable = local_jsonable
        api._id_reference_payload = lambda block: reference_payload(blend, block)
    return MODULE.register_commands(api)


# --------------------------------------------------------------------------- #
# fixture: one geometry-nodes group with two objects sharing it
# --------------------------------------------------------------------------- #
def standard_items():
    """Every interesting interface shape, in a fixed identifier order."""
    return [
        InterfaceItem("Size", "Socket_0", "NodeSocketFloat", default_value=1.0,
                      min_value=0.0, max_value=10.0, subtype="DISTANCE"),
        InterfaceItem("Density", "Socket_1", "NodeSocketInt", default_value=4,
                      min_value=0, max_value=64),
        InterfaceItem("Offset", "Socket_2", "NodeSocketVector", default_value=(0.0, 0.0, 0.0),
                      min_value=-5.0, max_value=5.0, subtype="TRANSLATION"),
        InterfaceItem("Flag", "Socket_3", "NodeSocketBool", default_value=True),
        InterfaceItem("Label", "Socket_4", "NodeSocketString", default_value="hi"),
        InterfaceItem("Geometry", "Socket_5", "NodeSocketGeometry", in_out="INPUT"),
        InterfaceItem("Target", "Socket_6", "NodeSocketObject", default_value=None,
                      pointer_rna="Object"),
        InterfaceItem("Switch", "Socket_7", "NodeSocketMenu", default_value="MODE_A",
                      enum_items=[("MODE_A", "A"), ("MODE_B", "B")]),
        InterfaceItem("Payload", "Socket_8", "NodeSocketBundle", default_value=0),
        InterfaceItem("Secret", "Socket_9", "NodeSocketFloat", default_value=1.0,
                      hide_in_modifier=True),
        InterfaceItem("Hidden", "Socket_10", "NodeSocketFloat", default_value=1.0,
                      hide_value=True),
        InterfaceItem("Linked", "Socket_11", "NodeSocketFloat", default_value=1.0),
        InterfaceItem("Field", "Socket_12", "NodeSocketFloat", default_value=1.0,
                      default_attribute_name=""),
        InterfaceItem("Driven", "Socket_13", "NodeSocketFloat", default_value=1.0),
        InterfaceItem("Indexed", "Socket_14", "NodeSocketMenu", default_value="MODE_A",
                      enum_rna_items=[("MODE_A", "A"), ("MODE_B", "B")]),
        InterfaceItem("Opaque", "Socket_15", "NodeSocketMenu", default_value="MYSTERY"),
        InterfaceItem("Output", "Socket_16", "NodeSocketGeometry", in_out="OUTPUT"),
        InterfaceItem("Panel", "Panel_0", item_type="PANEL"),
    ]


def standard_instance():
    """The per-instance store of one modifier instance over ``standard_items``."""
    return {
        "Socket_0": {"value": 1.0},
        "Socket_1": {"value": 4},
        "Socket_2": {"value": [0.0, 0.0, 0.0]},
        "Socket_3": {"value": True},
        "Socket_4": {"value": "hi"},
        "Socket_6": {"value": None},
        "Socket_7": {"value": "MODE_A"},
        "Socket_8": {"value": 0},
        "Socket_9": {"value": 1.0},
        "Socket_10": {"value": 1.0},
        "Socket_12": {"value": 1.0, "attribute_name": "", "type": 1},
        "Socket_13": {"value": 1.0},
        "Socket_14": {"value": 0},
        "Socket_15": {"value": "MYSTERY"},
    }


class Fixture(unittest.TestCase):
    """A two-object world whose objects share one node group."""

    def setUp(self):
        self.bind_world(standard_items(), standard_instance())

    def bind_world(self, items, instance, objects=("P", "Q"), nodes=(), with_converters=False):
        self.blend = FakeBlend()
        _STUB.data = self.blend
        _STUB.app = types.SimpleNamespace(version_string="5.2.1")
        self.scene = self.blend.add_scene("Main")
        self.group = self.blend.add_group("GN_Test", items, nodes)
        self.obj = self.blend.add_object(objects[0], self.group, dict(instance))
        self.other = self.blend.add_object(objects[1], self.group, dict(instance))
        self.helpers = bind(self.blend, with_converters)
        return self.group, self.obj

    # -- call helpers --------------------------------------------------- #
    def call(self, name, **params):
        return COMMANDS["properties_" + name](**params)

    def error(self, code, name, **params):
        with self.assertRaises(CommandFailure) as caught:
            self.call(name, **params)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def instance_of(self, obj=None, modifier="GN"):
        target = obj or self.obj
        mod = target.modifiers.get(modifier)
        return mod.properties.inputs

    def value_of(self, identifier, obj=None):
        return self.instance_of(obj)[identifier]["value"]

    def attribute_of(self, identifier, obj=None):
        return self.instance_of(obj)[identifier].to_dict().get("attribute_name", MISSING)

    def defaults(self):
        return {item.identifier: item.default_value for item in self.group.interface.items_tree
                if getattr(item, "item_type", "SOCKET") == "SOCKET"}

    def entry(self, payload, identifier):
        for item in payload["inputs"]:
            if item["identifier"] == identifier:
                return item
        self.fail("no entry for %r in %s" % (identifier, [i["identifier"] for i in payload["inputs"]]))


# --------------------------------------------------------------------------- #
# registration and listing
# --------------------------------------------------------------------------- #
class RegistrationTests(Fixture):
    def test_exactly_four_commands_are_registered(self):
        self.assertEqual(sorted(COMMANDS), sorted(COMMAND_NAMES))

    def test_mutating_flags_match_the_domain_contract(self):
        read_only = {"properties_list_parameters", "properties_get_parameter"}
        for name in COMMAND_NAMES:
            self.assertEqual(COMMANDS[name].bridge_mutating, name not in read_only, name)

    def test_helpers_dict_is_read_only_and_composable(self):
        for key in ("socket_tables", "resolve_target", "instance_keys", "interface_inputs",
                    "attribute_mode_state", "driver_reports", "enum_state", "describe_input",
                    "list_input_reports", "read_parameter", "write_gate", "view_layer_for"):
            self.assertIn(key, self.helpers)
        tables = self.helpers["socket_tables"]
        self.assertIn("NodeSocketFloat", tables["value_socket_types"])
        self.assertIn("NodeSocketGeometry", tables["read_only_socket_types"])
        self.assertEqual(tables["max_batch_changes"], 128)
        # Only read-only helpers are exported: no command callable is handed out.
        for value in self.helpers.values():
            self.assertNotIn(getattr(value, "__name__", ""), COMMAND_NAMES)

    def test_module_never_uses_operators_or_dynamic_execution(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for needle in ("bpy.ops.", "bpy.context.", "eval(", "exec(", "__import__"):
            self.assertNotIn(needle, source, "forbidden reference %r" % needle)
        # The stub has no ops/context attribute at all, so such an access would raise.
        self.assertIsNone(getattr(_STUB, "ops", None))
        self.assertIsNone(getattr(_STUB, "context", None))


class ListingTests(Fixture):
    def test_list_reports_every_documented_field(self):
        payload = self.call("list_parameters", object="P", modifier="GN")
        self.assertEqual(payload["object"], "P")
        self.assertEqual(payload["modifier"], "GN")
        self.assertEqual(payload["node_group"], "GN_Test")
        self.assertTrue(payload["per_instance_inputs"])
        self.assertIsNone(payload["target_readonly_reason"])
        size = self.entry(payload, "Socket_0")
        self.assertEqual(size["name"], "Size")
        self.assertEqual(size["socket_type"], "NodeSocketFloat")
        self.assertEqual(size["type"], "FLOAT")
        self.assertEqual(size["current"], 1.0)
        self.assertEqual(size["default"], 1.0)
        self.assertEqual((size["min"], size["max"]), (0.0, 10.0))
        self.assertEqual(size["subtype"], "DISTANCE")
        self.assertEqual(size["unit"], "LENGTH")
        self.assertEqual(size["unit_source"], "subtype_map")
        self.assertTrue(size["per_instance"])
        self.assertFalse(size["linked_inside_group"])
        self.assertTrue(size["field_capable_socket_type"])
        self.assertIsNone(size["is_field"])
        self.assertTrue(size["writable"])
        self.assertIsNone(size["readonly_reason"])
        self.assertEqual(size["default_owner"], "node_group_interface")
        self.assertTrue(size["default_is_shared"])
        self.assertEqual(size["current_owner"], "modifier_instance")
        self.assertEqual(size["attribute_mode"]["keys"], ["value"])
        self.assertFalse(size["attribute_mode"]["supported"])

    def test_not_per_instance_and_outputs_are_reported(self):
        payload = self.call("list_parameters", object="P", modifier="GN")
        self.assertIn("Socket_11", payload["not_per_instance"])
        self.assertIn("Socket_16", payload["outputs"])
        self.assertEqual(payload["input_count"], 16)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["panels"][0]["identifier"], "Panel_0")
        self.assertEqual(payload["duplicate_identifiers"], [])
        self.assertEqual(payload["blender_version"], "5.2.1")

    def test_unique_modifier_fallback_and_explicit_name(self):
        payload = self.call("list_parameters", object="P")
        self.assertEqual(payload["modifier"], "GN")
        named = self.call("list_parameters", object="P", modifier="GN")
        self.assertEqual(named["modifier"], "GN")

    def test_enum_items_are_read_from_the_live_rna(self):
        payload = self.call("list_parameters", object="P", modifier="GN")
        by_item = self.entry(payload, "Socket_7")
        self.assertEqual(by_item["enum"]["source"], "interface_item.enum_items")
        self.assertEqual(by_item["enum"]["identifiers"], ["MODE_A", "MODE_B"])
        self.assertEqual(by_item["enum_representation"]["representation"], "identifier")
        by_rna = self.entry(payload, "Socket_14")
        self.assertEqual(by_rna["enum"]["source"], "default_value_rna_enum")
        self.assertEqual(by_rna["enum_representation"]["representation"], "enum_value")
        opaque = self.entry(payload, "Socket_15")
        self.assertIsNone(opaque["enum"]["source"])
        self.assertFalse(opaque["writable"])
        self.assertEqual(opaque["readonly_reason"], "unsupported_enum")

    def test_gate_and_report_agree_on_every_input(self):
        payload = self.call("list_parameters", object="P", modifier="GN")
        expected_reason = {
            "Socket_5": "read_only_socket_type",   # output geometry socket
            "Socket_8": "unsupported_socket_type",  # bundle
            "Socket_9": "hidden_input",             # hide_in_modifier
            "Socket_10": "hidden_input",            # hide_value
            "Socket_11": "input_not_exposed",       # linked inside the group
            "Socket_15": "unsupported_enum",        # unproven menu representation
        }
        for identifier in ("Socket_5", "Socket_8", "Socket_9", "Socket_10", "Socket_11",
                           "Socket_15"):
            entry = self.entry(payload, identifier)
            self.assertFalse(entry["writable"], identifier)
            self.assertEqual(entry["readonly_reason"], expected_reason[identifier], identifier)
        for identifier in ("Socket_0", "Socket_1", "Socket_2", "Socket_3", "Socket_4", "Socket_6",
                           "Socket_7", "Socket_12", "Socket_13", "Socket_14"):
            self.assertTrue(self.entry(payload, identifier)["writable"], identifier)

    def test_linked_inside_group_is_visible(self):
        items = standard_items()
        node = types.SimpleNamespace(
            bl_idname="NodeGroupInput",
            outputs=[types.SimpleNamespace(identifier="Socket_11", is_linked=True),
                     types.SimpleNamespace(identifier="Socket_0", is_linked=False)],
        )
        self.bind_world(items, standard_instance(), nodes=[node])
        payload = self.call("list_parameters", object="P", modifier="GN")
        self.assertTrue(self.entry(payload, "Socket_11")["linked_inside_group"])
        self.assertFalse(self.entry(payload, "Socket_0")["linked_inside_group"])

    def test_drivers_are_reported_and_protected(self):
        self.obj.animation_data = AnimationData([
            DriverCurve('modifiers["GN"].properties.inputs["Socket_13"].value', "frame"),
        ])
        payload = self.call("list_parameters", object="P", modifier="GN")
        entry = self.entry(payload, "Socket_13")
        self.assertFalse(entry["writable"])
        self.assertEqual(entry["readonly_reason"], "read_only_driven")
        self.assertEqual(entry["drivers"][0]["owner"], "object")
        self.assertEqual(entry["drivers"][0]["matched"], "exact")
        # A driver on another object's modifier path must not block this one.
        self.assertTrue(self.entry(payload, "Socket_0")["writable"])

    def test_attribute_mode_state_comes_from_the_live_keys(self):
        payload = self.call("list_parameters", object="P", modifier="GN")
        proven = self.entry(payload, "Socket_12")
        self.assertTrue(proven["attribute_mode"]["supported"])
        self.assertTrue(proven["attribute_writable"])
        self.assertEqual(proven["attribute_mode"]["mode"], "VALUE")
        absent = self.entry(payload, "Socket_0")
        self.assertFalse(absent["attribute_mode"]["supported"])
        self.assertFalse(absent["attribute_writable"])
        self.assertNotIn("attribute_name", absent["attribute_mode"]["keys"])


# --------------------------------------------------------------------------- #
# target resolution
# --------------------------------------------------------------------------- #
class TargetTests(Fixture):
    def test_missing_object_is_reported(self):
        self.error("object_not_found", "list_parameters", object="Ghost", modifier="GN")

    def test_empty_object_or_modifier_name_is_refused(self):
        self.error("invalid_argument", "list_parameters", object="", modifier="GN")
        self.error("invalid_argument", "list_parameters", object="P", modifier="  ")

    def test_missing_modifier_lists_the_available_ones(self):
        failure = self.error("modifier_not_found", "list_parameters", object="P", modifier="Nope")
        self.assertEqual(failure.details["modifiers"], [{"name": "GN", "type": "NODES"}])

    def test_ambiguous_modifier_without_a_name_is_refused(self):
        self.obj.modifiers.append(Modifier(self.blend, "GN2", "NODES", self.group,
                                          standard_instance()))
        failure = self.error("ambiguous_modifier", "list_parameters", object="P")
        self.assertEqual(sorted(failure.details["geometry_nodes_modifiers"]), ["GN", "GN2"])
        self.assertEqual(self.call("list_parameters", object="P", modifier="GN2")["modifier"], "GN2")

    def test_wrong_modifier_type_is_refused(self):
        self.obj.modifiers.append(Modifier(self.blend, "Smooth", "SUBSURF", None, None,
                                          with_properties=False))
        self.error("wrong_modifier_type", "list_parameters", object="P", modifier="Smooth")
        self.error("modifier_not_found", "list_parameters", object="P", modifier="Ghost")

    def test_modifier_without_a_node_group_is_refused(self):
        self.obj.modifiers.append(Modifier(self.blend, "Empty", "NODES", None, {}))
        self.error("no_node_group", "list_parameters", object="P", modifier="Empty")

    def test_wrong_node_group_type_is_refused(self):
        group = self.blend.add_group("ShaderLike", standard_items())
        group.bl_rna = types.SimpleNamespace(identifier="ShaderNodeTree")
        self.obj.modifiers.append(Modifier(self.blend, "Shader", "NODES", group,
                                          standard_instance()))
        self.error("wrong_node_group_type", "list_parameters", object="P", modifier="Shader")

    def test_missing_per_instance_store_is_refused_for_writes(self):
        self.obj.modifiers.clear()
        self.obj.modifiers.append(Modifier(self.blend, "GN", "NODES", self.group, {},
                                          with_properties=False))
        failure = self.error("unsupported_blender_version", "set_parameter", object="P",
                             modifier="GN", identifier="Socket_0", value=2.0)
        self.assertEqual(failure.details["blender"], "5.2.1")
        payload = self.call("list_parameters", object="P", modifier="GN")
        self.assertFalse(payload["per_instance_inputs"])
        self.assertTrue(payload["per_instance_note"])

    def test_duplicate_identifier_is_refused(self):
        items = standard_items()
        items.append(InterfaceItem("Size again", "Socket_0", "NodeSocketFloat"))
        self.bind_world(items, standard_instance())
        failure = self.error("ambiguous_identifier", "get_parameter", object="P", modifier="GN",
                             identifier="Socket_0")
        self.assertIn("Socket_0", failure.message)
        payload = self.call("list_parameters", object="P", modifier="GN")
        self.assertEqual(payload["duplicate_identifiers"], ["Socket_0"])


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
class ReadTests(Fixture):
    def test_get_parameter_returns_the_current_value(self):
        payload = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_1")
        self.assertEqual(payload["object"], "P")
        parameter = payload["parameter"]
        self.assertEqual(parameter["identifier"], "Socket_1")
        self.assertEqual(parameter["current"], 4)
        self.assertEqual(parameter["default"], 4)

    def test_unknown_identifier_lists_the_available_ones(self):
        failure = self.error("input_not_found", "get_parameter", object="P", modifier="GN",
                             identifier="Socket_99")
        identifiers = [item["identifier"] for item in failure.details["available"]]
        self.assertIn("Socket_0", identifiers)
        self.assertNotIn("Panel_0", identifiers)

    def test_output_socket_is_refused_as_a_modifier_input(self):
        self.error("not_an_input", "get_parameter", object="P", modifier="GN",
                   identifier="Socket_16")
        self.error("invalid_argument", "get_parameter", object="P", modifier="GN", identifier="")

    def test_reads_never_touch_the_dependency_graph(self):
        before = self.blend.depsgraph_updates
        self.call("list_parameters", object="P", modifier="GN")
        self.call("get_parameter", object="P", modifier="GN", identifier="Socket_0")
        self.assertEqual(self.blend.depsgraph_updates, before)
        self.assertEqual(self.obj.update_tag_calls, 0)
        self.assertEqual(self.scene.update_tag_calls, 0)

    def test_pointer_input_reads_back_as_a_reference(self):
        material = Block("Steel", "Material")
        self.blend.materials.append(material)
        self.instance_of()["Socket_6"]["value"] = material
        payload = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_6")
        self.assertEqual(payload["parameter"]["current"],
                         {"id_type": "MATERIAL", "id_name": "Steel", "rna_type": "Material"})

    def test_reads_are_identical_with_the_addon_converters_present(self):
        self.bind_world(standard_items(), standard_instance(), with_converters=True)
        payload = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_2")
        self.assertEqual(payload["parameter"]["current"], [0.0, 0.0, 0.0])


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
class WriteTests(Fixture):
    def test_write_touches_only_the_instance_value(self):
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                            value=6.5)
        self.assertEqual(self.value_of("Socket_0"), 6.5)
        self.assertEqual(self.obj.update_tag_calls, 1)
        diff = payload["parameter"]
        self.assertEqual(diff["before"]["value"], 1.0)
        self.assertEqual(diff["after"]["value"], 6.5)
        self.assertEqual(diff["written_keys"], ["value"])
        self.assertTrue(diff["changed"])
        self.assertEqual(payload["changed_count"], 1)
        self.assertFalse(payload["all_idempotent"])
        self.assertEqual(payload["requested_count"], 1)

    def test_shared_default_and_other_object_are_untouched(self):
        defaults_before = self.defaults()
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0", value=7.0)
        self.assertEqual(self.defaults(), defaults_before)
        self.assertEqual(self.group.interface.items_tree[0].default_value, 1.0)
        self.assertEqual(self.value_of("Socket_0"), 7.0)
        self.assertEqual(self.value_of("Socket_0", self.other), 1.0)
        self.assertEqual(self.instance_of(self.other).to_dict(),
                         {key: value for key, value in standard_instance().items()})

    def test_read_back_verification_and_idempotence(self):
        first = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                          value=2.0)
        self.assertTrue(first["parameter"]["changed"])
        again = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                          value=2.0)
        self.assertFalse(again["parameter"]["changed"])
        self.assertTrue(again["all_idempotent"])
        self.assertEqual(again["changed_count"], 0)

    def test_scalar_and_vector_types_are_strict_and_coerced(self):
        self.call("set_parameter", object="P", identifier="Socket_0", value=3)
        self.assertEqual(self.value_of("Socket_0"), 3.0)
        self.call("set_parameter", object="P", identifier="Socket_1", value=4.0)
        self.assertEqual(self.value_of("Socket_1"), 4)
        self.call("set_parameter", object="P", identifier="Socket_3", value=False)
        self.assertIs(self.value_of("Socket_3"), False)
        self.call("set_parameter", object="P", identifier="Socket_4", value="hello")
        self.assertEqual(self.value_of("Socket_4"), "hello")
        payload = self.call("set_parameter", object="P", identifier="Socket_2",
                            value=[1, 2.5, 3])
        self.assertEqual(self.value_of("Socket_2"), [1.0, 2.5, 3.0])
        self.assertTrue(payload["parameter"]["coerced"])

    def test_color_and_rotation_arity(self):
        items = standard_items() + [
            InterfaceItem("Tint", "Socket_20", "NodeSocketColor", default_value=(0, 0, 0, 1)),
            InterfaceItem("Spin", "Socket_21", "NodeSocketRotation", default_value=(0, 0, 0)),
        ]
        instance = standard_instance()
        instance["Socket_20"] = {"value": [0.0, 0.0, 0.0, 1.0]}
        instance["Socket_21"] = {"value": [0.0, 0.0, 0.0]}
        self.bind_world(items, instance)
        self.call("set_parameter", object="P", identifier="Socket_20", value=[1, 0.5, 0, 1])
        self.assertEqual(self.value_of("Socket_20"), [1.0, 0.5, 0.0, 1.0])
        self.call("set_parameter", object="P", identifier="Socket_21", value=[0, 0, 1.5])
        self.assertEqual(self.value_of("Socket_21"), [0.0, 0.0, 1.5])
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_20",
                   value=[1, 0, 1])
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_21",
                   value=[0, 0, 0, 0])
        self.assertEqual(self.value_of("Socket_20"), [1.0, 0.5, 0.0, 1.0])

    def test_wrong_types_are_refused_without_writing(self):
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_0",
                   value=True)
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_0",
                   value="6")
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_1",
                   value=1.5)
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_1",
                   value=True)
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_3",
                   value=1)
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_4", value=7)
        self.error("invalid_argument", "set_parameter", object="P", identifier="Socket_0")
        self.assertEqual(self.value_of("Socket_0"), 1.0)
        self.assertEqual(self.value_of("Socket_1"), 4)
        self.assertIs(self.value_of("Socket_3"), True)
        self.assertEqual(self.value_of("Socket_4"), "hi")

    def test_declared_range_is_enforced(self):
        failure = self.error("value_out_of_range", "set_parameter", object="P",
                             identifier="Socket_0", value=99.0)
        self.assertEqual((failure.details["min"], failure.details["max"]), (0.0, 10.0))
        self.error("value_out_of_range", "set_parameter", object="P", identifier="Socket_1",
                   value=1000)
        self.error("value_out_of_range", "set_parameter", object="P", identifier="Socket_2",
                   value=[0, 0, 42])
        self.assertEqual(self.value_of("Socket_0"), 1.0)
        self.assertEqual(self.value_of("Socket_2"), [0.0, 0.0, 0.0])

    def test_string_socket_rejects_nul(self):
        self.error("invalid_value", "set_parameter", object="P", identifier="Socket_4",
                   value="a\x00b")

    def test_datablock_reference_is_typed_and_validated(self):
        target = self.blend.add_object("Cube")
        material = Block("Steel", "Material")
        self.blend.materials.append(material)
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_6",
                            value={"id_type": "OBJECT", "id_name": "Cube"})
        self.assertIs(self.value_of("Socket_6"), target)
        self.assertEqual(payload["parameter"]["after"]["value"],
                         {"id_type": "OBJECT", "id_name": "Cube", "rna_type": "Object"})
        # clearing with null is the documented way to empty a pointer input
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_6", value=None)
        self.assertIsNone(self.value_of("Socket_6"))
        # a plain string must never look like a successful write
        self.error("invalid_reference", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_6", value="Cube")
        self.error("reference_not_found", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_6", value={"id_type": "OBJECT", "id_name": "Ghost"})
        self.error("reference_type_mismatch", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_6", value={"id_type": "MATERIAL", "id_name": "Steel"})
        self.error("invalid_reference", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_6", value={"id_type": "SCENE", "id_name": "Main"})
        self.assertIsNone(self.value_of("Socket_6"))

    def test_material_pointer_accepts_a_material(self):
        items = standard_items()
        items.append(InterfaceItem("Surface", "Socket_22", "NodeSocketMaterial",
                                   default_value=None, pointer_rna="Material"))
        instance = standard_instance()
        instance["Socket_22"] = {"value": None}
        self.bind_world(items, instance)
        material = Block("Steel", "Material")
        self.blend.materials.append(material)
        self.call("set_parameter", object="P", identifier="Socket_22",
                  value={"id_type": "MATERIAL", "id_name": "Steel"})
        self.assertIs(self.value_of("Socket_22"), material)

    def test_hidden_inputs_are_readable_but_refused(self):
        hidden = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_9")
        self.assertEqual(hidden["parameter"]["current"], 1.0)
        failure = self.error("hidden_input", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_9", value=5.0)
        self.assertEqual(failure.details["reason"], "hide_in_modifier")
        value_hidden = self.error("hidden_input", "set_parameter", object="P", modifier="GN",
                                  identifier="Socket_10", value=5.0)
        self.assertEqual(value_hidden.details["reason"], "hide_value")
        self.assertEqual(self.value_of("Socket_9"), 1.0)
        self.assertEqual(self.value_of("Socket_10"), 1.0)

    def test_geometry_bundle_and_unexposed_inputs_are_refused(self):
        self.error("read_only_socket_type", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_5", value=1.0)
        self.error("unsupported_socket_type", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_8", value=1)
        failure = self.error("input_not_exposed", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_11", value=1.0)
        self.assertIn("Socket_0", failure.details["present"])
        self.assertEqual(self.value_of("Socket_8"), 0)

    def test_linked_and_overridden_targets_are_never_written(self):
        for field in ("library", "override_library"):
            self.bind_world(standard_items(), standard_instance())
            setattr(self.obj, field, object())
            failure = self.error("read_only_linked", "set_parameter", object="P",
                                 modifier="GN", identifier="Socket_0", value=5.0)
            self.assertEqual(failure.details["block"], "object")
            self.assertEqual(failure.details["reason"], field)
            self.assertEqual(self.value_of("Socket_0"), 1.0)
            payload = self.call("list_parameters", object="P", modifier="GN")
            self.assertEqual(payload["target_readonly_reason"], field)
            self.assertFalse(self.entry(payload, "Socket_0")["writable"])
            self.assertEqual(self.entry(payload, "Socket_0")["readonly_reason"], "read_only_linked")

    def test_linked_node_group_is_refused_too(self):
        self.group.library = object()
        failure = self.error("read_only_linked", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_0", value=5.0)
        self.assertEqual(failure.details["block"], "node_group")
        self.assertEqual(self.value_of("Socket_0"), 1.0)

    def test_driven_input_cannot_be_written(self):
        self.obj.animation_data = AnimationData([
            DriverCurve('modifiers["GN"].properties.inputs["Socket_0"].value', "frame * 2"),
        ])
        failure = self.error("read_only_driven", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_0", value=5.0)
        self.assertEqual(failure.details["drivers"][0]["expression"], "frame * 2")
        self.assertEqual(self.value_of("Socket_0"), 1.0)

    def test_menu_value_is_refused_when_representation_is_unproven(self):
        self.error("unsupported_enum", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_15", value="MYSTERY")
        self.assertEqual(self.value_of("Socket_15"), "MYSTERY")

    def test_menu_value_is_written_when_identifiers_and_shape_are_proven(self):
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_7",
                  value="MODE_B")
        self.assertEqual(self.value_of("Socket_7"), "MODE_B")
        # a display name maps to its identifier through the live enum
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_7", value="A")
        self.assertEqual(self.value_of("Socket_7"), "MODE_A")
        self.error("invalid_value", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_7", value="NOPE")
        # the index representation is written as an index
        self.call("set_parameter", object="P", modifier="GN", identifier="Socket_14", value=1)
        self.assertEqual(self.value_of("Socket_14"), 1)
        self.error("value_out_of_range", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_14", value=5)

    def test_attribute_mode_is_refused_without_proof_and_writes_no_key(self):
        keys_before = self.instance_of()["Socket_0"].keys()
        failure = self.error("unsupported_mode", "set_parameter", object="P", modifier="GN",
                             identifier="Socket_0", value=None, mode="ATTRIBUTE",
                             attribute_name="Position")
        self.assertEqual(failure.details["verified_on"], "live IDPropertyGroup keys")
        self.assertEqual(self.instance_of()["Socket_0"].keys(), keys_before)
        self.assertNotIn("attribute_name", self.instance_of()["Socket_0"].to_dict())

    def test_attribute_mode_requires_a_name_and_a_field_socket(self):
        self.error("invalid_argument", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", value=None, mode="ATTRIBUTE", attribute_name="")
        self.error("unsupported_mode", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_3", value=None, mode="ATTRIBUTE", attribute_name="Flag")
        self.error("invalid_argument", "set_parameter", object="P", modifier="GN",
                   identifier="Socket_12", mode="MAYBE", value=1.0)

    def test_attribute_mode_writes_the_proven_key(self):
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_12",
                            value=None, mode="ATTRIBUTE", attribute_name="Position")
        self.assertEqual(self.attribute_of("Socket_12"), "Position")
        self.assertEqual(self.value_of("Socket_12"), 1.0)
        diff = payload["parameter"]
        self.assertEqual(diff["mode"], "ATTRIBUTE")
        self.assertEqual(diff["written_keys"], ["attribute_name", "type"])
        self.assertEqual(self.instance_of()["Socket_12"]["type"], 2)
        self.assertEqual(diff["after"]["attribute_name"], "Position")
        self.assertTrue(diff["changed"])
        # and reading it back reports ATTRIBUTE mode
        read = self.call("get_parameter", object="P", modifier="GN", identifier="Socket_12")
        self.assertEqual(read["parameter"]["attribute_mode"]["mode"], "ATTRIBUTE")
        self.assertEqual(read["parameter"]["attribute_mode"]["attribute_name"], "Position")

    def test_value_mode_clears_the_observed_attribute_name(self):
        self.instance_of()["Socket_12"]["attribute_name"] = "Position"
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_12",
                            value=4.0)
        self.assertEqual(self.attribute_of("Socket_12"), "")
        self.assertEqual(self.value_of("Socket_12"), 4.0)
        self.assertEqual(payload["parameter"]["written_keys"], ["value", "type", "attribute_name"])
        self.assertEqual(self.instance_of()["Socket_12"]["type"], 1)
        self.assertEqual(payload["parameter"]["before"]["attribute_name"], "Position")
        self.assertIsNone(payload["parameter"]["after"]["attribute_name"])
        self.assertEqual(payload["parameter"]["attribute_mode_cleared"], "Position")

    def test_depsgraph_state_is_reported(self):
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                            value=3.0)
        status = payload["depsgraph"]
        self.assertTrue(status["object_update_tag"])
        self.assertEqual(status["scene"], "Main")
        self.assertTrue(status["scene_update_tag"])
        self.assertEqual(status["view_layer"], "ViewLayer")
        self.assertTrue(status["view_layer_updated"])
        self.assertEqual(status["depsgraph_source"], "view_layer")
        self.assertTrue(status["depsgraph_updated"])
        self.assertEqual(status["frame"], 1)
        self.assertEqual(status["evaluation"]["evaluated"]["vertices"], 8)
        self.assertEqual(status["signature"]["checksum"], 12345)
        self.assertEqual(status["warnings"], [])
        self.assertEqual(self.scene.update_tag_calls, 1)
        self.assertEqual(self.scene.view_layers[0].depsgraph.update_calls, 1)
        self.assertEqual(self.blend.stats_calls[0]["object"], "P")

    def test_context_depsgraph_fallback_is_reported(self):
        self.scene.view_layers[0].depsgraph = None
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                            value=3.0)
        self.assertEqual(payload["depsgraph"]["depsgraph_source"], "context")
        self.assertTrue(payload["depsgraph"]["depsgraph_updated"])
        self.assertEqual(self.value_of("Socket_0"), 3.0)

    def test_evaluation_failure_is_reported_honestly(self):
        self.blend.depsgraph_error = "injected depsgraph failure"
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                            value=3.0)
        status = payload["depsgraph"]
        self.assertEqual(self.value_of("Socket_0"), 3.0)
        self.assertFalse(status["depsgraph_updated"])
        self.assertIsNone(status["evaluation"])
        steps = [warning["step"] for warning in status["warnings"]]
        self.assertIn("depsgraph_update", steps)
        note = next(warning["note"] for warning in status["warnings"]
                    if warning["step"] == "depsgraph_update")
        self.assertIn("could not be proven", note)
        self.assertEqual(payload["warnings"], status["warnings"])

    def test_statistics_and_signature_errors_do_not_hide_the_write(self):
        self.blend.stats_error = "no mesh"
        self.blend.signature_error = "no signature"
        payload = self.call("set_parameter", object="P", modifier="GN", identifier="Socket_0",
                            value=3.0)
        steps = [warning["step"] for warning in payload["warnings"]]
        self.assertEqual(steps, ["evaluated_mesh_stats", "mesh_signature"])
        self.assertTrue(payload["depsgraph"]["depsgraph_updated"])
        self.assertEqual(self.value_of("Socket_0"), 3.0)


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #
class BatchTests(Fixture):
    def test_precheck_failure_leaves_zero_side_effects(self):
        snapshot = self.instance_of().to_dict()
        failure = self.error(
            "batch_precheck_failed", "set_parameters", object="P", modifier="GN",
            changes=[{"identifier": "Socket_0", "value": 5.0},
                     {"identifier": "Socket_11", "value": 2.0}],
        )
        self.assertEqual([entry["identifier"] for entry in failure.details["failures"]],
                         ["Socket_11"])
        self.assertEqual(failure.details["failures"][0]["code"], "input_not_exposed")
        self.assertEqual(self.instance_of().to_dict(), snapshot)
        self.assertEqual(self.obj.update_tag_calls, 0)
        self.assertEqual(self.blend.depsgraph_updates, 0)

    def test_single_change_batch_reports_the_same_code_as_set_parameter(self):
        failure = self.error("value_out_of_range", "set_parameters", object="P", modifier="GN",
                             changes=[{"identifier": "Socket_0", "value": 500.0}])
        self.assertEqual(failure.details["identifier"], "Socket_0")

    def test_duplicate_identifier_is_refused(self):
        self.error("duplicate_identifier", "set_parameters", object="P", modifier="GN",
                   changes=[{"identifier": "Socket_0", "value": 2.0},
                            {"identifier": "Socket_0", "value": 3.0}])
        self.assertEqual(self.value_of("Socket_0"), 1.0)

    def test_batch_limit_is_enforced_before_the_preflight(self):
        changes = [{"identifier": "Socket_%d" % index, "value": 1.0} for index in range(129)]
        failure = self.error("invalid_argument", "set_parameters", object="P", modifier="GN",
                             changes=changes)
        self.assertEqual(failure.details["max"], 128)
        self.assertEqual(self.value_of("Socket_0"), 1.0)

    def test_empty_or_malformed_changes_are_refused(self):
        self.error("invalid_argument", "set_parameters", object="P", modifier="GN", changes=[])
        self.error("invalid_argument", "set_parameters", object="P", modifier="GN", changes=None)
        self.error("invalid_change", "set_parameters", object="P", modifier="GN",
                   changes=[{"identifier": "Socket_0", "value": 1.0, "extra": True}])
        self.error("invalid_change", "set_parameters", object="P", modifier="GN",
                   changes=[{"value": 1.0}])

    def test_batch_applies_and_reports_a_diff_per_input(self):
        payload = self.call("set_parameters", object="P", modifier="GN", changes=[
            {"identifier": "Socket_0", "value": 2.0},
            {"identifier": "Socket_1", "value": 8, "mode": "VALUE"},
            {"identifier": "Socket_12", "value": None, "mode": "ATTRIBUTE",
             "attribute_name": "Position"},
        ])
        self.assertEqual(payload["applied_count"], 3)
        self.assertEqual(payload["changed_count"], 3)
        self.assertEqual([diff["identifier"] for diff in payload["applied"]],
                         ["Socket_0", "Socket_1", "Socket_12"])
        self.assertEqual(payload["applied"][2]["written_keys"], ["attribute_name", "type"])
        self.assertEqual(self.value_of("Socket_0"), 2.0)
        self.assertEqual(self.value_of("Socket_1"), 8)
        self.assertEqual(self.attribute_of("Socket_12"), "Position")
        self.assertEqual(list(payload["parameters"]), ["Socket_0", "Socket_1", "Socket_12"])
        # exactly one evaluation for the whole batch
        self.assertEqual(len(self.blend.stats_calls), 1)
        self.assertEqual(self.obj.update_tag_calls, 1)
        self.assertEqual(self.value_of("Socket_0", self.other), 1.0)

    def test_injected_write_failure_restores_everything(self):
        failed = {"done": False}

        def hook(identifier, key, value):
            if identifier == "Socket_1" and not failed["done"]:
                failed["done"] = True  # one-shot: the store recovers, as a real one does
                return True
            return False

        self.blend.write_hook = hook
        failure = self.error("batch_write_failed", "set_parameters", object="P", modifier="GN",
                             changes=[{"identifier": "Socket_0", "value": 2.0},
                                      {"identifier": "Socket_1", "value": 9},
                                      {"identifier": "Socket_2", "value": [1.0, 1.0, 1.0]}])
        self.assertEqual(failure.details["rolled_back"], True)
        self.assertEqual(failure.details["failures"][0]["identifier"], "Socket_1")
        self.assertEqual(self.value_of("Socket_0"), 1.0)
        self.assertEqual(self.value_of("Socket_1"), 4)
        self.assertEqual(self.value_of("Socket_2"), [0.0, 0.0, 0.0])
        restored = {entry["identifier"]: entry["keys"] for entry in failure.details["restored"]}
        self.assertEqual(restored["Socket_0"], ["value"])
        self.assertEqual(failure.details["unsettled"], [])
        self.assertEqual(self.obj.update_tag_calls, 0)

    def test_restoration_covers_value_and_attribute_keys(self):
        self.instance_of()["Socket_12"]["attribute_name"] = "Position"
        failed = {"done": False}

        def hook(identifier, key, value):
            if identifier == "Socket_1" and not failed["done"]:
                failed["done"] = True
                return True
            return False

        self.blend.write_hook = hook
        failure = self.error("batch_write_failed", "set_parameters", object="P", modifier="GN",
                             changes=[{"identifier": "Socket_12", "value": 3.0},
                                      {"identifier": "Socket_1", "value": 9}])
        self.assertEqual(failure.details["rolled_back"], True)
        self.assertEqual(self.value_of("Socket_12"), 1.0)
        self.assertEqual(self.attribute_of("Socket_12"), "Position")

    def test_read_back_mismatch_rolls_the_whole_batch_back(self):
        corrupted = {"done": False}

        def corrupt(identifier, key, value):
            if (identifier, key) == ("Socket_1", "value") and not corrupted["done"]:
                corrupted["done"] = True
                return 0.5  # a store that silently kept a different value
            return value

        self.blend.corrupt_hook = corrupt
        failure = self.error("write_verification_failed", "set_parameters", object="P",
                             modifier="GN",
                             changes=[{"identifier": "Socket_0", "value": 2.0},
                                      {"identifier": "Socket_1", "value": 9}])
        self.assertEqual(failure.details["failures"][0]["identifier"], "Socket_1")
        self.assertEqual(failure.details["failures"][0]["read_back"], 0.5)
        self.assertEqual(failure.details["rolled_back"], True)
        self.assertEqual(self.value_of("Socket_0"), 1.0)
        self.assertEqual(self.value_of("Socket_1"), 4)

    def test_unsettled_keys_are_reported_instead_of_claimed_as_rolled_back(self):
        socket0 = self.instance_of()["Socket_0"]

        def hook(identifier, key, value):
            if identifier == "Socket_1":
                # Model a store that loses the sibling key between write and rollback.
                socket0._data.pop("value", None)
                return True
            return False

        self.blend.write_hook = hook
        failure = self.error("batch_write_failed", "set_parameters", object="P", modifier="GN",
                             changes=[{"identifier": "Socket_0", "value": 2.0},
                                      {"identifier": "Socket_1", "value": 9}])
        self.assertEqual(failure.details["rolled_back"], False)
        self.assertEqual(failure.details["unsettled"][0]["identifier"], "Socket_0")
        self.assertIn("incomplete", failure.details["note"])

    def test_idempotent_batch_reports_no_change(self):
        payload = self.call("set_parameters", object="P", modifier="GN", changes=[
            {"identifier": "Socket_0", "value": 1.0},
            {"identifier": "Socket_1", "value": 4},
        ])
        self.assertEqual(payload["changed_count"], 0)
        self.assertTrue(payload["all_idempotent"])
        self.assertFalse(payload["applied"][0]["changed"])


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
    EXPECTED = dict(zip(TOOL_NAMES, COMMAND_NAMES))

    @classmethod
    def setUpClass(cls):
        sys.modules.pop("bpy", None)  # the bridge harness temporarily stubs it
        from blender_mcp import audit
        from blender_mcp.tools import AuditedFastMCP
        from blender_mcp.tools import properties as tools

        audit.configure(directory=RUNTIME / "properties_test_audit", source="test")
        cls.server = AuditedFastMCP(name="properties-tests")
        cls.bridge = StubBridge()
        tools.register(cls.server, cls.bridge)
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.server.list_tools())}

    def test_four_named_tools_with_typed_schemas(self):
        self.assertEqual(sorted(self.tools), sorted(self.EXPECTED))
        listing = self.tools["blender_properties_list_parameters"].inputSchema
        self.assertEqual(listing["required"], ["object"])
        self.assertEqual(listing["properties"]["object"]["maxLength"], 255)
        self.assertEqual(listing["properties"]["modifier"]["default"], None)
        get = self.tools["blender_properties_get_parameter"].inputSchema
        self.assertEqual(get["required"], ["object", "identifier"])
        self.assertEqual(get["properties"]["identifier"]["minLength"], 1)
        single = self.tools["blender_properties_set_parameter"].inputSchema
        self.assertEqual(single["required"], ["object", "identifier"])
        self.assertEqual(sorted(single["properties"]["mode"]["enum"]), ["ATTRIBUTE", "VALUE"])
        self.assertEqual(single["properties"]["mode"]["default"], "VALUE")
        self.assertEqual(single["properties"]["value"]["default"], None)
        batch = self.tools["blender_properties_set_parameters"].inputSchema
        self.assertEqual(batch["required"], ["object", "changes"])
        self.assertEqual(batch["properties"]["changes"]["minItems"], 1)
        self.assertEqual(batch["properties"]["changes"]["maxItems"], 128)

    def test_title_annotations_and_domain_meta(self):
        read_only = {"blender_properties_list_parameters", "blender_properties_get_parameter"}
        for name in TOOL_NAMES:
            tool = self.tools[name]
            self.assertTrue(tool.title, name)
            self.assertFalse(tool.annotations.destructiveHint, name)
            self.assertFalse(tool.annotations.openWorldHint, name)
            self.assertEqual(tool.meta["domain"], "properties", name)
            self.assertEqual(tool.meta["bridge_prefix"], "properties_", name)
            self.assertIs(tool.meta["writes_shared_node_group_default"], False, name)
            self.assertIs(tool.meta["deletes_data"], False, name)
            self.assertEqual(tool.annotations.readOnlyHint, name in read_only, name)
            self.assertEqual(tool.meta["mutating"], name not in read_only, name)

    def test_forwarding_is_typed_and_never_imports_bpy(self):
        self.assertNotIn("bpy", sys.modules)
        cases = (
            ("blender_properties_list_parameters", {"object": "P", "modifier": "GN"},
             "properties_list_parameters", {"object": "P", "modifier": "GN"}),
            ("blender_properties_get_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_0"},
             "properties_get_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_0"}),
            ("blender_properties_set_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_0", "value": 2.5,
              "mode": "VALUE"},
             "properties_set_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_0", "value": 2.5,
              "mode": "VALUE", "attribute_name": None}),
            ("blender_properties_set_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_12", "value": None,
              "mode": "ATTRIBUTE", "attribute_name": "Position"},
             "properties_set_parameter",
             {"object": "P", "modifier": "GN", "identifier": "Socket_12", "value": None,
              "mode": "ATTRIBUTE", "attribute_name": "Position"}),
            ("blender_properties_set_parameters",
             {"object": "P", "modifier": "GN",
              "changes": [{"identifier": "Socket_0", "value": 3.0, "mode": "VALUE"},
                          {"identifier": "Socket_12", "value": None, "mode": "ATTRIBUTE",
                           "attribute_name": "Position"}]},
             "properties_set_parameters",
             {"object": "P", "modifier": "GN",
              "changes": [{"identifier": "Socket_0", "value": 3.0, "mode": "VALUE"},
                          {"identifier": "Socket_12", "mode": "ATTRIBUTE",
                           "attribute_name": "Position"}]}),
        )
        for tool_name, arguments, command, expected in cases:
            payload = _structured(asyncio.run(self.server.call_tool(tool_name, arguments)))
            self.assertIs(payload.get("ok"), True, tool_name)
            self.assertEqual(self.bridge.calls[-1]["command"], command, tool_name)
            self.assertEqual(self.bridge.calls[-1]["params"], expected, tool_name)

    def test_schema_rejects_bad_calls_before_the_bridge(self):
        cases = (
            ("blender_properties_list_parameters", {"object": ""}),
            ("blender_properties_list_parameters", {"object": "P", "modifier": 7}),
            ("blender_properties_get_parameter", {"object": "P"}),
            ("blender_properties_set_parameter",
             {"object": "P", "identifier": "Socket_0", "mode": "SIDEWAYS"}),
            ("blender_properties_set_parameters", {"object": "P", "changes": []}),
            ("blender_properties_set_parameters",
             {"object": "P",
              "changes": [{"identifier": "Socket_%d" % index, "value": 1.0}
                          for index in range(129)]}),
            ("blender_properties_set_parameters",
             {"object": "P", "changes": [{"identifier": "Socket_0", "value": 1.0,
                                          "nonsense": True}]}),
            ("blender_properties_set_parameters", {"object": "P", "changes": "Socket_0"}),
        )
        for tool_name, arguments in cases:
            before = len(self.bridge.calls)
            with self.assertRaises(Exception, msg="%s %s" % (tool_name, arguments)):
                asyncio.run(self.server.call_tool(tool_name, arguments))
            self.assertEqual(len(self.bridge.calls), before, tool_name)

    def test_registering_the_domain_changes_no_existing_tool_schema(self):
        from blender_mcp.tools import build_server
        from blender_mcp.tools import domains
        from blender_mcp.tools import properties as tools

        class Stub:
            def call(self, *args, **kwargs):
                return {}

            def close(self):
                pass

        before = {tool.name: tool.model_dump(exclude={"description"})
                  for tool in asyncio.run(build_server(Stub(), profile="all").list_tools())}
        original = domains.REGISTRATIONS

        def register_properties_tools(mcp, client, names):
            tools.register(mcp, client)

        domains.REGISTRATIONS = original + (register_properties_tools,)
        try:
            after = {tool.name: tool.model_dump(exclude={"description"})
                     for tool in asyncio.run(build_server(Stub(), profile="all").list_tools())}
        finally:
            domains.REGISTRATIONS = original
        # Properties is now part of the real default build. Registering it
        # again must preserve the same public surface and legacy schemas.
        self.assertEqual(set(after), set(before))
        self.assertTrue(set(TOOL_NAMES) <= set(after))
        for name, schema in before.items():
            self.assertEqual(after[name], schema, name)

    def test_registering_the_domain_does_not_import_bpy(self):
        from blender_mcp.tools import properties as tools
        self.assertTrue(callable(tools.register))
        self.assertNotIn("bpy", sys.modules)
        source = TOOLS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)


# --------------------------------------------------------------------------- #
# seam and wiring contracts
# --------------------------------------------------------------------------- #
class SeamTests(unittest.TestCase):
    def test_the_addon_exposes_every_helper_the_module_requires(self):
        source = ADDON_PATH.read_text(encoding="utf-8")
        for name in ("def command(", "class CommandError", "def get_object(",
                     "def flush_object(", "def depsgraph_update(",
                     "def evaluated_mesh_stats(", "def mesh_signature(",
                     "def is_reference_value(", "def resolve_id_reference(",
                     "def validate_reference_target(", "def _jsonable(",
                     "def _id_reference_payload(", "REFERENCE_ID_TYPES",
                     "REFERENCE_RNA_TYPES"):
            self.assertIn(name, source, "add-on helper %r is missing" % name)

    def test_bridge_module_registers_the_four_named_commands(self):
        bind(FakeBlend())
        self.assertEqual(sorted(COMMANDS), sorted(COMMAND_NAMES))

    def test_profile_mapping_is_fail_closed_until_the_domain_is_mapped(self):
        from blender_mcp import tool_profiles

        missing = [name for name in TOOL_NAMES if name not in tool_profiles.TOOL_DOMAINS]
        if missing:
            # Until the domain mapping is added next to the registration hook, a
            # restricted profile must refuse to start instead of silently hiding or
            # exposing the tools - that is the documented fail-closed contract.
            with self.assertRaises(tool_profiles.ToolProfileConfigurationError):
                tool_profiles.apply_tool_profile(
                    _StubMcp(), tool_profiles.resolve_profile("geometry_nodes"),
                    set(tool_profiles.KNOWN_TOOL_NAMES) | set(TOOL_NAMES),
                )
        else:
            for name in TOOL_NAMES:
                self.assertEqual(tool_profiles.TOOL_DOMAINS[name], "properties", name)


class _StubMcp:
    """Minimal FastMCP stand-in for the profile filter (only ``remove_tool``)."""

    def __init__(self):
        self.removed = []

    def remove_tool(self, name):
        self.removed.append(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
