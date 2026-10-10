"""Per-instance Geometry Nodes modifier parameters (Blender 5.2.1 IDProperty store).

What this module is
-------------------
The bridge command module behind the ``blender_properties_*`` MCP tools.  It is
imported by ``blender_mcp_addon._register_properties_commands()`` and publishes its
commands through ``register_commands(api)``; it imports only ``bpy`` plus the
standard library, never the ``mcp`` package, so a stock Blender can load it.

It edits exactly one thing: the **per-instance** input of one Geometry Nodes
modifier on one object, i.e. the value Blender keeps in
``modifier.properties.inputs[<identifier>]``.  It never writes the shared node
group interface default (``node_group.interface.items_tree[...].default_value``),
never another object's modifier, never the node graph, and never a file on disk.

Verified API basis (Blender 5.2.1 LTS)
--------------------------------------
* The per-instance store is ``modifier.properties.inputs`` - an IDPropertyGroup
  whose keys are the interface socket *identifiers* - and the value lives under the
  key ``value``.  This is the layout the pre-existing ``_set_modifier_parameters``
  command uses and that ``docs/GN_REFERENCES.md`` records as measured on this
  machine.
* Writing that ID property does **not** invalidate the modifier by itself: the
  owning object must be tagged (``object.update_tag()``) before a dependency graph
  update returns rebuilt geometry.  This module therefore tags the object, the
  owning view layer and (when the build exposes it) the owning scene, then
  re-evaluates and reports the real evaluation state - or the exact step that
  failed - instead of claiming a synchronous update.
* Socket identifiers are the only lookup key.  Display names are localized and
  version dependent and duplicates exist, so a name is never guessed into an
  identifier.

Deliberately conservative parts (see docs/ALGORITHM_REVIEW.md)
-------------------------------------------------------
* The **attribute (field) mode** of an input and **menu (enum) values** are written
  only when the generated input RNA enum and existing ``type``/``attribute_name``
  keys prove the mode selector, or the menu identifiers and stored representation
  are known. An attribute name alone never proves the active mode.
  Nothing is written into a key that was not observed at runtime; an
  unproven mode is refused with ``unsupported_mode`` / ``unsupported_enum``.
  Whether Blender 5.2.1 offers a per-instance attribute mode at all is *not* proven
  on this machine, so it is never assumed.
* ``NodeSocketGeometry`` and ``NodeSocketBundle`` inputs are refused by value.
* Hidden inputs (``hide_in_modifier``; and ``hide_value`` for VALUE mode) are
  readable but refused for writes.  The legacy ``blender_set_modifier_input`` keeps
  its old, looser behaviour and is untouched.
* Linked/overridden objects, node groups and modifiers, and inputs driven by an
  F-Curve driver, are read-only here.
"""

from __future__ import annotations

import json
import math

import bpy

# --------------------------------------------------------------------------- #
# bounds and tables (no bpy access, no guessing)
# --------------------------------------------------------------------------- #
#: One batch call never carries more than this many parameter changes.
MAX_BATCH_CHANGES = 128
#: One ``properties_list_parameters`` answer never reports more than this many inputs.
MAX_INPUTS = 512
#: Sanity cap for a string socket value; Blender's own (version dependent) limit is
#: caught by the read-back verification instead of being claimed here.
MAX_STRING_BYTES = 4096

#: The key inside one per-instance input group that carries the value.
VALUE_KEY = "value"
#: Key names that may carry "this input reads an attribute" state.  A key is used
#: only when the live group already exposes it; this tuple is not a promise that any
#: of these exists in this Blender build.
ATTRIBUTE_NAME_KEYS = ("attribute_name",)
#: Accepted values of the ``mode`` argument.
MODE_VALUES = ("VALUE", "ATTRIBUTE")

#: Exact per-component arity for the multi-component value sockets.
VECTOR_ARITY = {"NodeSocketVector": 3, "NodeSocketColor": 4, "NodeSocketRotation": 3}
#: Sockets whose per-instance value is a plain JSON value.
VALUE_SOCKET_TYPES = (
    "NodeSocketFloat",
    "NodeSocketInt",
    "NodeSocketBool",
    "NodeSocketVector",
    "NodeSocketColor",
    "NodeSocketRotation",
    "NodeSocketString",
)
#: Socket families that may support fields. Actual mode support must be proven
#: by the generated input RNA; this table alone never establishes ``is_field``.
FIELD_SOCKET_TYPES = VALUE_SOCKET_TYPES
#: Sockets whose per-instance value is a datablock pointer.
POINTER_SOCKET_TYPES = (
    "NodeSocketObject",
    "NodeSocketMaterial",
    "NodeSocketCollection",
    "NodeSocketImage",
)
#: Sockets that exist in the interface but can never be set by value.
READ_ONLY_SOCKET_TYPES = ("NodeSocketGeometry",)
#: Sockets this module refuses outright.
UNSUPPORTED_SOCKET_TYPES = ("NodeSocketBundle",)
#: Enum sockets: only writable when the live store proves the representation.
ENUM_SOCKET_TYPES = ("NodeSocketMenu",)
WRITABLE_SOCKET_TYPES = VALUE_SOCKET_TYPES + POINTER_SOCKET_TYPES + ENUM_SOCKET_TYPES

#: Subtype -> Blender unit system, for display only (``subtype`` stays authoritative).
SUBTYPE_UNITS = {
    "DISTANCE": "LENGTH",
    "TRANSLATION": "LENGTH",
    "ANGLE": "ROTATION",
    "TIME": "TIME",
    "TIME_ABSOLUTE": "TIME",
    "VELOCITY": "VELOCITY",
    "ACCELERATION": "ACCELERATION",
    "MASS": "MASS",
    "TEMPERATURE": "TEMPERATURE",
    "COLOR_TEMPERATURE": "TEMPERATURE",
    "POWER": "POWER",
    "FREQUENCY": "FREQUENCY",
    "WAVELENGTH": "WAVELENGTH",
    "FACTOR": "NONE",
    "PERCENTAGE": "NONE",
    "PIXEL": "PIXEL",
}

#: Read-back comparison tolerance (ID properties store float32).
FLOAT_TOLERANCE = 1e-5


# --------------------------------------------------------------------------- #
# pure helpers (no bpy, no api)
# --------------------------------------------------------------------------- #
def _local_jsonable(value):
    """Best-effort JSON conversion, mirroring the add-on's private ``_jsonable``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, dict):
        return {str(key): _local_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_local_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "to_dict"):  # IDPropertyGroup
        try:
            return {str(key): _local_jsonable(item) for key, item in value.to_dict().items()}
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "to_tuple"):  # mathutils Vector/Matrix/Euler/Quaternion
        try:
            return [float(item) for item in value.to_tuple()]
        except Exception:  # noqa: BLE001
            pass
    try:
        return [float(item) for item in value]
    except Exception:  # noqa: BLE001
        return str(value)


def _finite(value):
    """``float(value)`` when it is a finite real number, else ``None`` (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _short_type(socket_type):
    """``NodeSocketFloat`` -> ``FLOAT`` (display only)."""
    if not isinstance(socket_type, str) or not socket_type.startswith("NodeSocket"):
        return socket_type
    return socket_type[len("NodeSocket"):].upper()


def _declared_range(item):
    """Finite ``(min, max)`` declared by the interface socket, else ``(None, None)``.

    A range is used only when it is finite and not inverted; a socket that declares
    no meaningful range is not clamped.
    """
    low = _finite(getattr(item, "min_value", None))
    high = _finite(getattr(item, "max_value", None))
    if low is None or high is None or high <= low:
        return None, None
    return low, high


def _copy_value(value):
    """Deep copy of an ID-property value (a snapshot must not alias live lists)."""
    for method in ("to_list", "to_tuple"):
        converter = getattr(value, method, None)
        if callable(converter):
            value = list(converter())
            break
    if isinstance(value, (list, tuple)):
        return [_copy_value(item) for item in value]
    return value


def _same_value(expected, actual):
    """Compare a written value with what Blender stored (tolerant for float32)."""
    expected, actual = _copy_value(expected), _copy_value(actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= FLOAT_TOLERANCE * max(
            1.0, abs(float(expected)), abs(float(actual))
        )
    if isinstance(expected, str) or isinstance(actual, str):
        return expected == actual
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        return all(_same_value(left, right) for left, right in zip(expected, actual))
    return expected == actual


def _number_text(number):
    """``1.0`` -> ``1`` for messages; everything else unchanged."""
    return int(number) if float(number).is_integer() else number


def _strict_same(left, right):
    """Type-aware comparison, so ``[1, 2]`` and ``[1.0, 2.0]`` differ (for the
    "the value you passed was converted" flag)."""
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(_strict_same(a, b) for a, b in zip(left, right)))
    return type(left) is type(right) and left == right


def _same_datablock(left, right):
    if left is right:
        return True
    first, second = getattr(left, "as_pointer", None), getattr(right, "as_pointer", None)
    return callable(first) and callable(second) and first() == second()


def _plain(value):
    """Comparable plain form of a read-back payload (a reference becomes a tuple)."""
    if isinstance(value, dict) and "id_name" in value:
        return ("reference", value.get("id_type"), value.get("id_name"))
    return value


def register_commands(api):
    """Publish the ``properties_*`` bridge commands and return read-only helpers.

    ``api`` is the add-on module: it supplies ``CommandError``, the ``command``
    decorator, the dependency-graph/tagging helpers and the datablock-reference
    helpers (``is_reference_value`` / ``resolve_id_reference`` /
    ``validate_reference_target``).  The returned dict exposes **read-only**
    helpers only, so another command module can compose on them without gaining a
    write path.
    """
    error = api.CommandError

    def _jsonable(value):
        """Prefer the add-on's converter; fall back to the local equivalent."""
        converter = getattr(api, "_jsonable", None)
        return converter(value) if callable(converter) else _local_jsonable(value)

    def _reference_payload(block):
        """Prefer the add-on's reference serialiser, with a local fallback."""
        converter = getattr(api, "_id_reference_payload", None)
        if callable(converter):
            return converter(block)
        if block is None:
            return None
        rna_name = getattr(getattr(block, "bl_rna", None), "identifier", None)
        inverse = {value: key for key, value in dict(getattr(api, "REFERENCE_RNA_TYPES", {}) or {}).items()}
        return {
            "id_type": inverse.get(rna_name, rna_name or type(block).__name__),
            "id_name": getattr(block, "name", None),
            "rna_type": rna_name,
        }

    # -- argument validation ------------------------------------------------- #
    def _text(value, label):
        """An exact, non-empty Blender name (max 255 UTF-8 bytes, no NUL)."""
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise error("invalid_argument", "%s must be a non-empty exact name" % label)
        if len(value.encode("utf-8")) > 255:
            raise error("invalid_argument", "%s must be at most 255 UTF-8 bytes" % label)
        return value

    def _mode(value):
        if value is None:
            return "VALUE"
        if not isinstance(value, str) or value.upper() not in MODE_VALUES:
            raise error(
                "invalid_argument",
                "mode must be 'VALUE' or 'ATTRIBUTE'",
                details={"received": _jsonable(value), "accepted": list(MODE_VALUES)},
            )
        return value.upper()

    # -- target resolution --------------------------------------------------- #
    def _modifier_named(obj, modifier):
        """The one exact NODES modifier named ``modifier`` (or the only NODES one)."""
        nodes_modifiers = [item for item in obj.modifiers if getattr(item, "type", None) == "NODES"]
        available = [{"name": item.name, "type": item.type} for item in obj.modifiers]
        if modifier is None or modifier == "":
            if len(nodes_modifiers) == 1:
                return nodes_modifiers[0]
            if not nodes_modifiers:
                raise error(
                    "modifier_not_found",
                    "object %r has no Geometry Nodes modifier" % obj.name,
                    details={"object": obj.name, "modifiers": available},
                )
            raise error(
                "ambiguous_modifier",
                "object %r has %d Geometry Nodes modifiers; pass modifier="
                % (obj.name, len(nodes_modifiers)),
                details={"object": obj.name,
                         "geometry_nodes_modifiers": [item.name for item in nodes_modifiers]},
            )
        if not isinstance(modifier, str):
            raise error("invalid_argument", "modifier must be an exact modifier name")
        _text(modifier, "modifier")
        matches = [item for item in obj.modifiers if item.name == modifier]
        if len(matches) > 1:
            raise error(
                "ambiguous_modifier",
                "object %r has %d modifiers named %r" % (obj.name, len(matches), modifier),
            )
        if not matches:
            raise error(
                "modifier_not_found",
                "object %r has no modifier named %r" % (obj.name, modifier),
                details={"object": obj.name, "modifiers": available},
            )
        target = matches[0]
        if getattr(target, "type", None) != "NODES":
            raise error(
                "wrong_modifier_type",
                "modifier %r on %r is %s, not a Geometry Nodes (NODES) modifier"
                % (modifier, obj.name, getattr(target, "type", None)),
                details={"object": obj.name, "modifier": modifier,
                         "geometry_nodes_modifiers": [item.name for item in nodes_modifiers]},
            )
        return target

    def resolve_target(object_name, modifier=None):
        """``(object, modifier, node_group)`` for one exact object+modifier pair."""
        _text(object_name, "object")
        obj = api.get_object(object_name)
        target = _modifier_named(obj, modifier)
        group = getattr(target, "node_group", None)
        if group is None:
            raise error("no_node_group",
                        "modifier %r on %r has no node group" % (target.name, obj.name))
        rna = getattr(getattr(group, "bl_rna", None), "identifier", None)
        if rna is not None and rna != "GeometryNodeTree":
            raise error(
                "wrong_node_group_type",
                "modifier %r uses %s, not a GeometryNodeTree" % (target.name, rna),
                details={"object": obj.name, "modifier": target.name, "node_group": group.name,
                         "rna_type": rna},
            )
        return obj, target, group

    # -- instance store ------------------------------------------------------ #
    def instance_inputs(modifier):
        """The live per-instance IDPropertyGroup, or ``None`` on this build."""
        return getattr(getattr(modifier, "properties", None), "inputs", None)

    def _group_keys(group):
        try:
            return [str(key) for key in group.keys()]
        except Exception:  # noqa: BLE001
            return []

    def instance_keys(inputs):
        """The identifier keys actually present on this modifier instance."""
        return [] if inputs is None else _group_keys(inputs)

    def _group_value(group, key):
        if group is None:
            return None
        try:
            return group[key]
        except Exception:  # noqa: BLE001
            return None

    def _snapshot_group(group):
        """Every key/value of one per-instance input, for exact restoration."""
        keys = _group_keys(group)
        data = {}
        for key in keys:
            try:
                data[key] = _copy_value(group[key])
            except Exception as exc:  # noqa: BLE001
                raise error("snapshot_failed", "cannot snapshot input key %r: %s" % (key, exc))
        return {"keys": keys, "data": data}

    def _restore_group(group, snapshot):
        """Write a snapshot back; returns ``(restored_keys, unsettled_keys)``."""
        restored, unsettled = [], []
        present = set(_group_keys(group))
        for key in snapshot["keys"]:
            if key not in present:
                unsettled.append("%s (key no longer present)" % key)
                continue
            try:
                group[key] = _copy_value(snapshot["data"].get(key))
                if _same_value(group[key], snapshot["data"][key]):
                    restored.append(key)
                else:
                    unsettled.append("%s (restoration read-back mismatch)" % key)
            except Exception as exc:  # noqa: BLE001
                unsettled.append("%s (%s)" % (key, exc))
        return restored, unsettled

    # -- interface ----------------------------------------------------------- #
    def interface_inputs(group):
        """``(inputs_by_identifier, outputs_by_identifier, panels, duplicate_ids)``."""
        inputs, outputs, panels, duplicates = {}, {}, [], []
        for item in group.interface.items_tree:
            kind = getattr(item, "item_type", "SOCKET")
            identifier = getattr(item, "identifier", None)
            in_out = getattr(item, "in_out", None)
            if kind == "PANEL":
                panels.append({"name": getattr(item, "name", None), "identifier": identifier,
                               "in_out": in_out})
                continue
            if not identifier:
                continue
            table = inputs if in_out == "INPUT" else outputs
            if identifier in table:
                duplicates.append(identifier)
                continue
            table[identifier] = item
        return inputs, outputs, panels, duplicates

    def _linked_inside_group(group):
        """Identifiers of group inputs that are linked to a node inside the group."""
        linked = set()
        for node in group.nodes:
            if getattr(node, "bl_idname", None) != "NodeGroupInput":
                continue
            for socket in node.outputs:
                if getattr(socket, "is_linked", False):
                    identifier = getattr(socket, "identifier", None)
                    if identifier:
                        linked.add(str(identifier))
        return linked

    def _input_item(group, identifier):
        """The interface INPUT item for one identifier (strict, never by name)."""
        inputs, outputs, _panels, duplicates = interface_inputs(group)
        if identifier in duplicates:
            raise error(
                "ambiguous_identifier",
                "node group %r declares the input identifier %r more than once"
                % (group.name, identifier),
            )
        item = inputs.get(identifier)
        if item is not None:
            return item
        if identifier in outputs:
            raise error(
                "not_an_input",
                "%r is an OUTPUT socket of node group %r, not a modifier input"
                % (identifier, group.name),
            )
        raise error(
            "input_not_found",
            "node group %r exposes no input with the identifier %r" % (group.name, identifier),
            details={
                "node_group": group.name,
                "available": [
                    {"identifier": key, "name": getattr(value, "name", None),
                     "socket_type": getattr(value, "socket_type", None)}
                    for key, value in sorted(inputs.items())
                ],
            },
        )

    # -- per-instance state -------------------------------------------------- #
    def attribute_mode_state(group, identifier, rna_input=None):
        """Use the generated input RNA enum, never infer mode from a saved name."""
        keys = _group_keys(group)
        state = {
            "supported": False,
            "verified": False,
            "mode": None,
            "attribute_name": None,
            "attribute_name_key": None,
            "mode_key": None,
            "mode_values": {},
            "keys": keys,
            "other_keys": [key for key in keys if key not in (VALUE_KEY,) + ATTRIBUTE_NAME_KEYS],
            "note": (
                "mode requires the live input type enum and its existing storage key; "
                "an attribute name alone does not prove the selected input mode"
            ),
        }
        if VALUE_KEY not in keys:
            state["note"] = ("this input exposes no %r key; the storage shape is unknown"
                             % VALUE_KEY)
            return state
        for key in ATTRIBUTE_NAME_KEYS:
            if key not in keys:
                continue
            raw = _group_value(group, key)
            if not isinstance(raw, str):
                state["note"] = "key %r exists but does not hold a string" % key
                return state
            state["attribute_name_key"] = key
            state["attribute_name"] = raw or None
            break
        if "type" not in keys:
            return state
        rna = getattr(rna_input if rna_input is not None else group, "bl_rna", None)
        properties = getattr(rna, "properties", None)
        prop = properties.get("type") if properties is not None else None
        if prop is None or getattr(prop, "type", None) != "ENUM":
            return state
        try:
            values = {entry.identifier: entry.value for entry in prop.enum_items
                      if entry.identifier}
        except Exception:  # noqa: BLE001
            return state
        raw_type = _group_value(group, "type")
        selected = [name for name, value in values.items() if value == raw_type]
        state["mode_values"] = values
        state["mode_key"] = "type"
        if len(selected) != 1:
            state["note"] = "stored type does not match exactly one live RNA enum value"
            return state
        state["mode"] = selected[0]
        state["verified"] = True
        state["supported"] = ("ATTRIBUTE" in values
                              and state["attribute_name_key"] is not None)
        state["note"] = "selected mode and allowed modes come from the generated input RNA enum"
        return state

    def input_mode(inputs, identifier):
        return attribute_mode_state(inputs[identifier], identifier,
                                    getattr(inputs, identifier, None))

    def input_visibility(modifier, identifier):
        report = {}
        for method, key in (("is_input_visible", "visible"), ("is_input_used", "used")):
            callback = getattr(modifier, method, None)
            try:
                report[key] = bool(callback(identifier)) if callable(callback) else None
            except Exception:  # noqa: BLE001
                report[key] = None
        return report

    def enum_state(item, group, rna_input=None):
        """Reliably discoverable menu identifiers for a ``NodeSocketMenu`` input."""
        items, source = [], None
        rna = getattr(rna_input if rna_input is not None else group, "bl_rna", None)
        properties = getattr(rna, "properties", None)
        prop = properties.get(VALUE_KEY) if properties is not None else None
        if prop is not None and getattr(prop, "type", None) == "ENUM":
            try:
                items = [{"identifier": entry.identifier, "name": entry.name,
                          "value": entry.value} for entry in prop.enum_items if entry.identifier]
                source = "modifier_input_value_rna_enum" if items else None
            except Exception:  # noqa: BLE001
                items, source = [], None
        raw = getattr(item, "enum_items", None)
        if raw is not None and not items:
            try:
                for entry in raw:
                    identifier = getattr(entry, "identifier", None) or getattr(entry, "name", None)
                    if isinstance(identifier, str) and identifier:
                        items.append({"identifier": identifier,
                                      "name": getattr(entry, "name", identifier),
                                      "value": getattr(entry, "value", None)})
                source = "interface_item.enum_items" if items else None
            except Exception:  # noqa: BLE001
                items, source = [], None
        if not items:
            rna = getattr(item, "bl_rna", None)
            properties = getattr(rna, "properties", None)
            prop = properties.get("default_value") if properties is not None else None
            if prop is not None and getattr(prop, "type", None) == "ENUM":
                try:
                    for entry in prop.enum_items:
                        if entry.identifier:
                            items.append({"identifier": entry.identifier, "name": entry.name,
                                          "value": getattr(entry, "value", None)})
                    source = "default_value_rna_enum" if items else None
                except Exception:  # noqa: BLE001
                    items, source = [], None
        if not items or len({entry["identifier"] for entry in items}) != len(items):
            return {"items": [], "identifiers": [], "source": None,
                    "note": "no reliable enum source on this input"}
        return {"items": items, "identifiers": [entry["identifier"] for entry in items],
                "source": source,
                "note": "identifiers come from the live RNA, not from a hardcoded table"}

    def _enum_representation(group):
        """What the live store proves about how a menu value is stored."""
        keys = _group_keys(group)
        if VALUE_KEY not in keys:
            return {"representation": None, "current": None,
                    "note": "no stored value yet; representation unproven"}
        raw = _group_value(group, VALUE_KEY)
        if isinstance(raw, str):
            return {"representation": "identifier", "current": raw,
                    "note": "the stored value is a string"}
        if isinstance(raw, int) and not isinstance(raw, bool):
            return {"representation": "enum_value", "current": raw,
                    "note": "an integer enum value requires a verified RNA value map; it is not a menu index"}
        return {"representation": None, "current": _jsonable(raw),
                "note": "the stored value is neither a string nor an integer"}

    def driver_reports(obj, group, modifier, identifier):
        """Drivers that would overwrite a write to this input (object or node group)."""
        candidates = (
            'modifiers[%s].properties.inputs.%s.value' %
            (json.dumps(modifier.name, ensure_ascii=False), identifier),
            'modifiers[%s].properties.inputs.%s.type' %
            (json.dumps(modifier.name, ensure_ascii=False), identifier),
            'modifiers[%s].properties.inputs.%s.attribute_name' %
            (json.dumps(modifier.name, ensure_ascii=False), identifier),
            'modifiers["%s"].properties.inputs["%s"].%s' % (modifier.name, identifier, VALUE_KEY),
            'modifiers["%s"].properties.inputs["%s"]' % (modifier.name, identifier),
            'modifiers["%s"]["%s"]' % (modifier.name, identifier),
        )
        found = []
        for owner, label in ((obj, "object"), (group, "node_group")):
            animation = getattr(owner, "animation_data", None)
            drivers = list(getattr(animation, "drivers", None) or []) if animation is not None else []
            for fcurve in drivers:
                path = str(getattr(fcurve, "data_path", "") or "")
                exact = path in candidates
                if not (exact or (identifier in path and modifier.name in path)):
                    continue
                driver = getattr(fcurve, "driver", None)
                found.append({
                    "owner": label,
                    "owner_name": getattr(owner, "name", None),
                    "data_path": path,
                    "array_index": getattr(fcurve, "array_index", None),
                    "expression": getattr(driver, "expression", None),
                    "is_valid": getattr(driver, "is_valid", None),
                    "matched": "exact" if exact else "modifier+identifier in data path",
                })
        return found

    def _readonly_target(obj, modifier, group):
        """Why nothing on this modifier may be written: ``(block_label, reason)``."""
        for label, block in (("object", obj), ("modifier", modifier), ("node_group", group)):
            if getattr(block, "library", None) is not None:
                return label, "library"
            if getattr(block, "override_library", None) is not None:
                return label, "override_library"
            if bool(getattr(block, "is_library_indirect", False)):
                return label, "library_indirect"
        return None, None

    # -- value validation ---------------------------------------------------- #
    def _pointer_property(item):
        rna = getattr(item, "bl_rna", None)
        properties = getattr(rna, "properties", None)
        prop = properties.get("default_value") if properties is not None else None
        if prop is not None and getattr(prop, "type", None) == "POINTER":
            return prop
        return None

    def _check_range(item, number, identifier, component=None):
        low, high = _declared_range(item)
        if low is None or low <= number <= high:
            return
        raise error(
            "value_out_of_range",
            "input %r declares the range [%s, %s]; %r is outside it"
            % (identifier, _number_text(low), _number_text(high), number),
            details={"identifier": identifier, "min": low, "max": high, "value": number,
                     "component": component},
        )

    def _coerce_enum(value, enum, representation, identifier):
        identifiers = enum["identifiers"]
        by_name = {entry["name"]: entry["identifier"] for entry in enum["items"]}
        if representation["representation"] == "enum_value":
            by_identifier = {entry["identifier"]: entry.get("value") for entry in enum["items"]}
            if any(isinstance(number, bool) or not isinstance(number, int)
                   for number in by_identifier.values()):
                raise error("unsupported_enum", "integer menu values cannot be verified for input %r" % identifier,
                            details={"identifier": identifier, "enum": enum})
            if isinstance(value, str) and value in by_identifier:
                return by_identifier[value]
            if isinstance(value, bool) or not isinstance(value, int):
                raise error(
                    "invalid_value",
                    "menu input %r stores an RNA enum value; pass a verified integer value or an identifier"
                    % identifier,
                    details={"identifier": identifier, "enum": enum,
                             "representation": representation},
                )
            if value not in by_identifier.values():
                raise error(
                    "value_out_of_range",
                    "menu value %r is not a verified enum value for input %r" % (value, identifier),
                    details={"identifier": identifier, "allowed_values": list(by_identifier.values())},
                )
            return int(value)
        if not isinstance(value, str) or not value:
            raise error(
                "invalid_value",
                "menu input %r stores an identifier string here; pass one of %s"
                % (identifier, identifiers),
                details={"identifier": identifier, "enum": enum},
            )
        if value in identifiers:
            return value
        if value in by_name:
            return by_name[value]
        raise error(
            "invalid_value",
            "%r is not a known identifier or name of menu input %r" % (value, identifier),
            details={"identifier": identifier, "enum": enum},
        )

    def _coerce_value(item, socket_type, value, identifier):
        """Strict coercion for a plain value socket; raises ``CommandError``."""
        if socket_type == "NodeSocketFloat":
            number = _finite(value)
            if number is None:
                raise error(
                    "invalid_value",
                    "input %r is a float socket; got %r" % (identifier, _jsonable(value)),
                )
            _check_range(item, number, identifier)
            return number
        if socket_type == "NodeSocketInt":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise error(
                    "invalid_value",
                    "input %r is an integer socket; got %r" % (identifier, _jsonable(value)),
                )
            if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
                raise error(
                    "invalid_value",
                    "input %r is an integer socket; %r is not a whole number" % (identifier, value),
                )
            number = int(value)
            _check_range(item, number, identifier)
            return number
        if socket_type == "NodeSocketBool":
            if not isinstance(value, bool):
                raise error(
                    "invalid_value",
                    "input %r is a boolean socket; got %r (strict: no 0/1 or \"true\")"
                    % (identifier, _jsonable(value)),
                )
            return bool(value)
        if socket_type == "NodeSocketString":
            if not isinstance(value, str):
                raise error(
                    "invalid_value",
                    "input %r is a string socket; got %r" % (identifier, _jsonable(value)),
                )
            if "\x00" in value:
                raise error("invalid_value", "input %r must not contain NUL" % identifier)
            if len(value.encode("utf-8")) > MAX_STRING_BYTES:
                raise error(
                    "invalid_value",
                    "input %r exceeds %d UTF-8 bytes" % (identifier, MAX_STRING_BYTES),
                )
            return value
        arity = VECTOR_ARITY.get(socket_type)
        if arity is None:
            raise error(
                "unsupported_socket_type",
                "input %r is a %s socket, which this server does not write"
                % (identifier, socket_type),
            )
        if not isinstance(value, (list, tuple)) or len(value) != arity:
            raise error(
                "invalid_value",
                "input %r (%s) needs exactly %d numbers, got %r"
                % (identifier, socket_type, arity, _jsonable(value)),
                details={"identifier": identifier, "socket_type": socket_type, "arity": arity},
            )
        components = []
        for index, component in enumerate(value):
            number = _finite(component)
            if number is None:
                raise error(
                    "invalid_value",
                    "input %r component %d is not a finite number: %r"
                    % (identifier, index, _jsonable(component)),
                )
            _check_range(item, number, identifier, component=index)
            components.append(number)
        return components

    # -- the single write gate (shared by the report and the planner) -------- #
    def write_gate(obj, modifier, group, item, socket_type, present, drivers, mode):
        """``(ok, code, message, details)`` for one write on one input.

        Used both to report ``writable``/``readonly_reason`` and to refuse a write,
        so the report and the enforcement can never disagree.
        """
        identifier = getattr(item, "identifier", None)
        label, reason = _readonly_target(obj, modifier, group)
        if reason is not None:
            name = getattr({"object": obj, "modifier": modifier, "node_group": group}[label],
                           "name", None)
            return False, "read_only_linked", (
                "%s %r is linked/overridden (%s); this server never writes linked data"
                % (label, name, reason)
            ), {"block": label, "reason": reason}
        if socket_type in READ_ONLY_SOCKET_TYPES:
            return False, "read_only_socket_type", (
                "input %r is a %s socket; it cannot be set by value" % (identifier, socket_type)
            ), {"identifier": identifier, "socket_type": socket_type}
        if socket_type in UNSUPPORTED_SOCKET_TYPES or socket_type not in WRITABLE_SOCKET_TYPES:
            return False, "unsupported_socket_type", (
                "input %r is a %s socket, which this server does not write"
                % (identifier, socket_type)
            ), {"identifier": identifier, "socket_type": socket_type,
                "supported": sorted(WRITABLE_SOCKET_TYPES)}
        if not present:
            return False, "input_not_exposed", (
                "input %r is not part of modifier %r's per-instance inputs; it is linked "
                "inside the node group, hidden, or not exposed by this Blender build"
                % (identifier, modifier.name)
            ), {"identifier": identifier, "socket_type": socket_type,
                "present": [key for key in instance_keys(instance_inputs(modifier))]}
        if drivers:
            return False, "read_only_driven", (
                "input %r is driven by an F-Curve driver; a manual write would be overwritten"
                % identifier
            ), {"identifier": identifier, "drivers": drivers}
        if bool(getattr(item, "hide_in_modifier", False)):
            return False, "hidden_input", (
                "input %r is hidden in the modifier panel (hide_in_modifier=true)" % identifier
            ), {"identifier": identifier, "reason": "hide_in_modifier"}
        if input_visibility(modifier, identifier)["visible"] is False:
            return False, "hidden_input", "input %r is currently hidden by modifier conditions" % identifier, {
                "identifier": identifier, "reason": "is_input_visible"}
        if mode == "VALUE" and bool(getattr(item, "hide_value", False)):
            return False, "hidden_input", (
                "input %r has no value widget on the modifier panel (hide_value=true); use "
                "mode='ATTRIBUTE' only when this build proves attribute mode for it"
                % identifier
            ), {"identifier": identifier, "reason": "hide_value"}
        return True, None, None, {}

    def _plan_change(obj, modifier, group, inputs, linked, drivers_cache, spec):
        """Full pre-flight for one change; returns a plan, writes nothing."""
        identifier = spec["identifier"]
        item = _input_item(group, identifier)
        socket_type = getattr(item, "socket_type", None)
        present = identifier in set(instance_keys(inputs))
        drivers = drivers_cache.setdefault(
            identifier, driver_reports(obj, group, modifier, identifier)
        )
        mode = spec["mode"]
        ok, code, message, details = write_gate(
            obj, modifier, group, item, socket_type, present, drivers, mode
        )
        if not ok:
            raise error(code, message, details=details)

        plan = {"index": spec["index"], "identifier": identifier, "mode": mode, "writes": [],
                "name": getattr(item, "name", None), "socket_type": socket_type,
                "linked_inside_group": identifier in linked, "expected": None,
                "coerced": False, "attribute_cleared": None}
        state = input_mode(inputs, identifier)
        if "type" in state["keys"]:
            if not state["verified"] or mode not in state["mode_values"]:
                raise error("unsupported_mode", "input %r cannot reliably select mode %s" %
                            (identifier, mode), details={"identifier": identifier, "state": state})
            plan["mode_value"] = state["mode_values"][mode]

        if mode == "ATTRIBUTE":
            if socket_type not in FIELD_SOCKET_TYPES:
                raise error(
                    "unsupported_mode",
                    "input %r is a %s socket; attribute mode applies to field value sockets only"
                    % (identifier, socket_type),
                    details={"identifier": identifier, "socket_type": socket_type,
                             "field_socket_types": sorted(FIELD_SOCKET_TYPES)},
                )
            attribute_name = spec["attribute_name"]
            if not isinstance(attribute_name, str) or not attribute_name.strip():
                raise error(
                    "invalid_argument",
                    "mode='ATTRIBUTE' needs a non-empty attribute_name",
                    details={"identifier": identifier, "received": _jsonable(attribute_name)},
                )
            if "\x00" in attribute_name:
                raise error("invalid_argument", "attribute_name must not contain NUL")
            if not state["supported"]:
                raise error(
                    "unsupported_mode",
                    "attribute mode is not proven for input %r in this Blender build: the "
                    "per-instance store exposes %s and no recognized attribute-name key"
                    % (identifier, state["keys"]),
                    details={"identifier": identifier, "mode": "ATTRIBUTE", "state": state,
                             "verified_on": "live IDPropertyGroup keys"},
                )
            plan["writes"].append({"key": state["attribute_name_key"], "value": attribute_name})
            plan["writes"].append({"key": state["mode_key"], "value": plan["mode_value"]})
            plan["expected"] = _group_value(inputs[identifier], VALUE_KEY)
            return plan

        value = spec["value"]
        if socket_type in POINTER_SOCKET_TYPES:
            if value is not None and not api.is_reference_value(value):
                raise error(
                    "invalid_reference",
                    "input %r is a %s socket: pass {\"id_type\": ..., \"id_name\": ...} or null "
                    "to clear it" % (identifier, socket_type),
                    details={"identifier": identifier, "received": _jsonable(value),
                             "supported": sorted(getattr(api, "REFERENCE_ID_TYPES", {}) or {})},
                )
            id_type, block = api.resolve_id_reference(value)
            prop = _pointer_property(item)
            if block is not None and prop is not None:
                api.validate_reference_target(prop, id_type, block, "modifier input %r" % identifier)
            plan["writes"].append({"key": VALUE_KEY, "value": block})
            plan["expected"] = block
            plan["reference"] = {"id_type": id_type,
                                 "id_name": getattr(block, "name", None) if block is not None else None}
            if state["verified"]:
                plan["writes"].append({"key": state["mode_key"], "value": plan["mode_value"]})
            return plan

        if socket_type in ENUM_SOCKET_TYPES:
            enum = enum_state(item, inputs[identifier], getattr(inputs, identifier, None))
            representation = _enum_representation(inputs[identifier])
            if not enum["items"]:
                raise error(
                    "unsupported_enum",
                    "input %r is a menu socket and its enum identifiers cannot be verified here"
                    % identifier,
                    details={"identifier": identifier, "enum": enum,
                             "representation": representation},
                )
            if representation["representation"] is None:
                raise error(
                    "unsupported_enum",
                    "menu input %r has no proven stored representation; refusing to guess" % identifier,
                    details={"identifier": identifier, "enum": enum,
                             "representation": representation},
                )
            plan["writes"].append({"key": VALUE_KEY,
                                   "value": _coerce_enum(value, enum, representation, identifier)})
            plan["expected"] = plan["writes"][-1]["value"]
            plan["coerced"] = not _strict_same(plan["expected"], value)
            if state["verified"]:
                plan["writes"].append({"key": state["mode_key"], "value": plan["mode_value"]})
            return plan

        if value is None:
            raise error(
                "invalid_argument",
                "input %r is a %s socket; mode='VALUE' needs a value (null only clears a "
                "datablock input)" % (identifier, socket_type),
                details={"identifier": identifier, "socket_type": socket_type},
            )
        coerced = _coerce_value(item, socket_type, value, identifier)
        plan["writes"].append({"key": VALUE_KEY, "value": coerced})
        plan["expected"] = coerced
        plan["coerced"] = not _strict_same(coerced, value)
        if state["verified"]:
            plan["writes"].append({"key": state["mode_key"], "value": plan["mode_value"]})
        if state["attribute_name"] is not None:
            # Switching this input back to a single value: clear the observed
            # attribute name.  Only a key that already exists is ever written.
            plan["writes"].append({"key": state["attribute_name_key"], "value": ""})
            plan["attribute_cleared"] = state["attribute_name"]
        return plan

    # -- read-only description ----------------------------------------------- #
    def _serialise(socket_type, raw):
        if socket_type in POINTER_SOCKET_TYPES:
            if raw is None or isinstance(raw, (int, float, str, bool, list)):
                return None
            return _reference_payload(raw)
        return _jsonable(raw)

    def _describe(obj, modifier, group, item, inputs, linked, identifier, drivers=None):
        """The read-only report for one interface input of one modifier instance."""
        socket_type = getattr(item, "socket_type", None)
        present = identifier in set(instance_keys(inputs))
        driver_list = (driver_reports(obj, group, modifier, identifier)
                       if drivers is None else drivers)
        instance = inputs[identifier] if inputs is not None and present else None
        state = (input_mode(inputs, identifier) if instance is not None else
                 attribute_mode_state(None, identifier))
        if instance is None:
            state = {"supported": False, "verified": False, "mode": "VALUE",
                     "attribute_name": None, "attribute_name_key": None, "keys": [],
                     "other_keys": [],
                     "note": "this input has no per-instance storage on this modifier"}
        writable, reason, _message, details = write_gate(
            obj, modifier, group, item, socket_type, present, driver_list, "VALUE"
        )
        if writable and "type" in state["keys"] and (
                not state["verified"] or "VALUE" not in state["mode_values"]):
            writable, reason = False, "unsupported_mode"
            details = {"identifier": identifier, "state": state}
        representation = None
        menu_enum = None
        if socket_type in ENUM_SOCKET_TYPES:
            menu_enum = enum_state(item, instance, getattr(inputs, identifier, None))
            representation = _enum_representation(instance) if instance is not None else {}
            enum_proven = bool(menu_enum.get("items")) and representation.get("representation") is not None
            if representation.get("representation") == "enum_value":
                enum_proven = enum_proven and all(
                    isinstance(entry.get("value"), int) and not isinstance(entry["value"], bool)
                    for entry in menu_enum["items"])
            if writable and not enum_proven:
                writable, reason = False, "unsupported_enum"
                details = {"identifier": identifier,
                           "enum": menu_enum,
                           "representation": representation,
                           "note": "menu values are written only when identifiers and the "
                                   "stored representation are both proven"}
        target_reason = _readonly_target(obj, modifier, group)[1]
        low, high = _declared_range(item)
        subtype = getattr(item, "subtype", None)
        attribute_writable = bool(
            present
            and socket_type in FIELD_SOCKET_TYPES
            and state["supported"]
            and not driver_list
            and not bool(getattr(item, "hide_in_modifier", False))
            and target_reason is None
            and input_visibility(modifier, identifier)["visible"] is not False
        )
        return {
            "identifier": identifier,
            "name": getattr(item, "name", None),
            "socket_type": socket_type,
            "type": _short_type(socket_type),
            "in_out": "INPUT",
            "description": getattr(item, "description", "") or "",
            "default": _serialise(socket_type, getattr(item, "default_value", None)),
            "default_owner": "node_group_interface",
            "default_is_shared": True,
            "current": _serialise(socket_type, _group_value(instance, VALUE_KEY)),
            "current_owner": "modifier_instance" if instance is not None else None,
            "per_instance": present,
            "linked_inside_group": identifier in linked,
            "min": low,
            "max": high,
            "subtype": subtype,
            "unit": SUBTYPE_UNITS.get(subtype) if isinstance(subtype, str) else None,
            "unit_source": ("subtype_map" if isinstance(subtype, str) and subtype in SUBTYPE_UNITS
                            else None),
            "hide_value": bool(getattr(item, "hide_value", False)),
            "hide_in_modifier": bool(getattr(item, "hide_in_modifier", False)),
            "default_attribute_name": getattr(item, "default_attribute_name", None),
            "field_capable_socket_type": socket_type in FIELD_SOCKET_TYPES,
            "structure_type": getattr(item, "structure_type", None),
            "is_field": (state["mode"] == "ATTRIBUTE"
                         if state["mode"] in ("VALUE", "ATTRIBUTE") else None),
            "supports_attribute_mode": state["supported"],
            "modifier_input_visibility": input_visibility(modifier, identifier),
            "enum": menu_enum,
            "enum_representation": representation,
            "attribute_mode": state,
            "writable": bool(writable),
            "readonly_reason": reason,
            "readonly_details": details,
            "attribute_writable": attribute_writable,
            "drivers": driver_list,
            "extra_keys": state["other_keys"],
        }

    def list_input_reports(obj, modifier, group, inputs=None):
        """Read-only reports for every exposed interface input of one instance."""
        inputs = instance_inputs(modifier) if inputs is None else inputs
        items, outputs, panels, duplicates = interface_inputs(group)
        linked = _linked_inside_group(group)
        drivers_cache = {}
        keys = sorted(items)
        entries = [
            _describe(obj, modifier, group, items[identifier], inputs, linked, identifier,
                      drivers_cache.setdefault(
                          identifier, driver_reports(obj, group, modifier, identifier)))
            for identifier in keys[:MAX_INPUTS]
        ]
        present = set(instance_keys(inputs))
        return {
            "object": obj.name,
            "modifier": modifier.name,
            "node_group": group.name,
            "is_geometry_nodes": getattr(getattr(group, "bl_rna", None), "identifier", None)
            in (None, "GeometryNodeTree"),
            "per_instance_inputs": inputs is not None,
            "target_readonly_reason": _readonly_target(obj, modifier, group)[1],
            "inputs": entries,
            "input_count": len(keys),
            "truncated": len(keys) > MAX_INPUTS,
            "not_per_instance": [identifier for identifier in keys if identifier not in present],
            "outputs": sorted(outputs),
            "panels": panels,
            "duplicate_identifiers": duplicates,
            "limits": {"max_inputs": MAX_INPUTS, "max_batch_changes": MAX_BATCH_CHANGES},
            "blender_version": getattr(getattr(bpy, "app", None), "version_string", None),
        }

    def read_parameter(object_name, modifier=None, identifier=None):
        """Read-only single-input report (the same shape as one list entry)."""
        if not isinstance(identifier, str) or not identifier:
            raise error("invalid_argument", "identifier must be a non-empty socket identifier")
        obj, target, group = resolve_target(object_name, modifier)
        inputs = instance_inputs(target)
        if inputs is None:
            raise error(
                "unsupported_blender_version",
                "this Blender build exposes no per-instance geometry-nodes inputs",
                details={"blender": getattr(getattr(bpy, "app", None), "version_string", None)},
            )
        item = _input_item(group, identifier)
        entry = _describe(obj, target, group, item, inputs, _linked_inside_group(group), identifier)
        return {"object": obj.name, "modifier": target.name, "node_group": group.name,
                "parameter": entry}

    # -- dependency graph ---------------------------------------------------- #
    def view_layer_for(obj):
        """``(scene, view_layer)`` that actually contains ``obj``, never a random one."""
        my_name = getattr(obj, "name", None)
        for scene in list(getattr(getattr(bpy, "data", None), "scenes", None) or []):
            layers = list(getattr(scene, "view_layers", None) or [])
            for layer in layers:
                getter = getattr(getattr(layer, "objects", None), "get", None)
                if callable(getter):
                    try:
                        if _same_datablock(getter(my_name), obj):
                            return scene, layer
                    except Exception:  # noqa: BLE001
                        continue
            getter = getattr(getattr(scene, "objects", None), "get", None)
            if callable(getter):
                try:
                    if _same_datablock(getter(my_name), obj):
                        return scene, (layers[0] if layers else None)
                except Exception:  # noqa: BLE001
                    continue
        return None, None

    def update_and_evaluate(obj):
        """Tag, update and re-evaluate ``obj``; report exactly what happened."""
        status = {
            "object_update_tag": False,
            "scene": None,
            "scene_update_tag": False,
            "view_layer": None,
            "view_layer_updated": False,
            "depsgraph_source": None,
            "depsgraph_updated": False,
            "frame": None,
            "evaluation": None,
            "signature": None,
            "warnings": [],
        }
        try:
            api.flush_object(obj)  # object.update_tag()
            status["object_update_tag"] = True
        except Exception as exc:  # noqa: BLE001
            status["warnings"].append({"step": "object_update_tag",
                                       "error": "%s: %s" % (type(exc).__name__, exc)})

        scene, layer = view_layer_for(obj)
        if scene is not None:
            status["scene"] = getattr(scene, "name", None)
            status["frame"] = getattr(scene, "frame_current", None)
            tag = getattr(scene, "update_tag", None)
            if callable(tag):
                try:
                    tag()
                    status["scene_update_tag"] = True
                except Exception as exc:  # noqa: BLE001
                    status["warnings"].append({"step": "scene_update_tag",
                                               "error": "%s: %s" % (type(exc).__name__, exc)})
        if layer is not None:
            status["view_layer"] = getattr(layer, "name", None)
            update = getattr(layer, "update", None)
            if not callable(update):
                status["warnings"].append({
                    "step": "view_layer_update",
                    "note": "this Blender build exposes no ViewLayer.update(); only the "
                            "object tag and the depsgraph update were used",
                })
            else:
                try:
                    update()
                    status["view_layer_updated"] = True
                except Exception as exc:  # noqa: BLE001
                    status["warnings"].append({"step": "view_layer_update",
                                               "error": "%s: %s" % (type(exc).__name__, exc)})

        deps = getattr(layer, "depsgraph", None) if layer is not None else None
        if deps is not None:
            status["depsgraph_source"] = "view_layer"
            try:
                deps.update()
                status["depsgraph_updated"] = True
            except Exception as exc:  # noqa: BLE001
                status["warnings"].append({"step": "view_layer_depsgraph_update",
                                           "error": "%s: %s" % (type(exc).__name__, exc)})
                deps = None
        if deps is None:
            try:
                deps = api.depsgraph_update()
                status["depsgraph_source"] = "context"
                status["depsgraph_updated"] = True
            except Exception as exc:  # noqa: BLE001
                status["warnings"].append({
                    "step": "depsgraph_update",
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "note": "the parameter was written, but re-evaluation could not be proven",
                })
                return status

        try:
            status["evaluation"] = api.evaluated_mesh_stats(obj, deps)
        except Exception as exc:  # noqa: BLE001
            status["warnings"].append({"step": "evaluated_mesh_stats",
                                       "error": "%s: %s" % (type(exc).__name__, exc)})
        signature = getattr(api, "mesh_signature", None)
        if callable(signature):
            try:
                status["signature"] = signature(obj, deps)
            except Exception as exc:  # noqa: BLE001
                status["warnings"].append({
                    "step": "mesh_signature",
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "note": "the evaluated statistics above are still the real ones",
                })
        return status

    # -- write execution ----------------------------------------------------- #
    def _apply_plan(group, plan):
        for write in plan["writes"]:
            group[write["key"]] = write["value"]
        return [write["key"] for write in plan["writes"]]

    def _verify_plan(group, plan):
        """Read-back verification of one applied plan: ``(ok, read_back)``."""
        actual = _group_value(group, VALUE_KEY)
        if "mode_value" in plan and _group_value(group, "type") != plan["mode_value"]:
            return False, {"value": _jsonable(actual), "type": _group_value(group, "type")}
        if plan["writes"][0]["key"] != VALUE_KEY:  # attribute-only change
            state = attribute_mode_state(group, plan["identifier"])
            expected = plan["writes"][0]["value"]
            return state["attribute_name"] == (expected or None), state["attribute_name"]
        if plan.get("attribute_cleared") is not None:
            # Switching back to a single value must really have cleared the observed
            # attribute name, otherwise the input is left in two states at once.
            state = attribute_mode_state(group, plan["identifier"])
            if state["attribute_name"] is not None:
                return False, {"value": _jsonable(actual),
                               "attribute_name": state["attribute_name"]}
        expected = plan["expected"]
        if isinstance(expected, str) or isinstance(actual, str):
            return _same_value(expected, actual), _jsonable(actual)
        if plan["socket_type"] in POINTER_SOCKET_TYPES:
            if expected is None:
                return actual is None, None if actual is None else _jsonable(actual)
            if actual is expected:
                return True, _serialise(plan["socket_type"], actual)
            pointer = getattr(actual, "as_pointer", None)
            wanted = getattr(expected, "as_pointer", None)
            same = callable(pointer) and callable(wanted) and pointer() == wanted()
            return same, _serialise(plan["socket_type"], actual)
        return _same_value(expected, actual), _jsonable(actual)

    def _change_specs(changes):
        """Validate the batch envelope (shape, count, keys, identifiers, duplicates)."""
        if changes is None or not isinstance(changes, (list, tuple)):
            raise error("invalid_argument", "changes must be a list of change objects")
        if not changes:
            raise error("invalid_argument", "changes must not be empty")
        if len(changes) > MAX_BATCH_CHANGES:
            raise error(
                "invalid_argument",
                "changes holds %d entries; at most %d are accepted"
                % (len(changes), MAX_BATCH_CHANGES),
                details={"count": len(changes), "max": MAX_BATCH_CHANGES},
            )
        accepted = {"identifier", "value", "mode", "attribute_name"}
        specs, seen = [], set()
        for index, raw in enumerate(changes):
            if not isinstance(raw, dict):
                raise error(
                    "invalid_change",
                    "changes[%d] must be an object with identifier/value/mode/attribute_name" % index,
                    details={"index": index, "received": _jsonable(raw)},
                )
            unknown = sorted(set(raw) - accepted)
            if unknown:
                raise error(
                    "invalid_change",
                    "changes[%d] has unknown key(s): %s" % (index, ", ".join(unknown)),
                    details={"index": index, "accepted": sorted(accepted)},
                )
            identifier = raw.get("identifier")
            if not isinstance(identifier, str) or not identifier:
                raise error(
                    "invalid_change",
                    "changes[%d].identifier must be a non-empty socket identifier" % index,
                    details={"index": index, "received": _jsonable(identifier)},
                )
            if identifier in seen:
                raise error(
                    "duplicate_identifier",
                    "changes[%d] repeats the identifier %r; at most one change per input"
                    % (index, identifier),
                    details={"index": index, "identifier": identifier},
                )
            seen.add(identifier)
            specs.append({"index": index, "identifier": identifier, "value": raw.get("value"),
                          "mode": _mode(raw.get("mode")),
                          "attribute_name": raw.get("attribute_name")})
        return specs

    def _prepare(obj, modifier, group, inputs, specs):
        """Full-batch pre-flight; nothing is written when any change is refused."""
        linked = _linked_inside_group(group)
        drivers_cache = {}
        plans, problems = [], []
        for spec in specs:
            try:
                plans.append(_plan_change(obj, modifier, group, inputs, linked, drivers_cache, spec))
            except error as exc:
                problems.append({"index": spec["index"], "identifier": spec["identifier"],
                                 "code": exc.code, "message": exc.message,
                                 "details": _jsonable(exc.details) if exc.details else None})
        if not problems:
            return plans
        if len(specs) == 1:
            raise error(problems[0]["code"], problems[0]["message"], details=problems[0]["details"])
        raise error(
            "batch_precheck_failed",
            "%d of %d changes were refused before anything was written"
            % (len(problems), len(specs)),
            details={"failures": problems, "written": [], "rolled_back": True},
        )

    def _rollback(inputs, plans, before, failures, code):
        """Restore every touched input (all keys), then raise with the honest report."""
        restored, unsettled = [], []
        for plan in plans:
            identifier = plan["identifier"]
            done, missing = _restore_group(inputs[identifier], before[identifier])
            restored.append({"identifier": identifier, "keys": done})
            if missing:
                unsettled.append({"identifier": identifier, "keys": missing})
        first = failures[0]
        detail = first.get("error") or first.get("message") or first.get("code")
        raise error(
            code,
            ("no change was kept: %s" if not unsettled else
             "write failed and restoration was incomplete: %s") % detail,
            details={
                "failures": failures,
                "restored": restored,
                "unsettled": unsettled,
                "rolled_back": not unsettled,
                "note": ("every touched input's value and mode keys were restored from the snapshot"
                         if not unsettled else
                         "restoration was incomplete; see 'unsettled' for the exact keys"),
            },
        )

    def _execute(obj, modifier, group, inputs, specs):
        """Pre-flight everything, apply, verify; roll back and raise on any failure."""
        plans = _prepare(obj, modifier, group, inputs, specs)
        before = {plan["identifier"]: _snapshot_group(inputs[plan["identifier"]]) for plan in plans}
        before_value = {plan["identifier"]: _serialise(
            plan["socket_type"], _group_value(inputs[plan["identifier"]], VALUE_KEY))
            for plan in plans}
        before_mode = {plan["identifier"]: input_mode(inputs, plan["identifier"]) for plan in plans}
        before_attribute = {key: state["attribute_name"] for key, state in before_mode.items()}

        failures, applied = [], []
        for plan in plans:
            try:
                _apply_plan(inputs[plan["identifier"]], plan)
            except Exception as exc:  # noqa: BLE001
                failures.append({"index": plan["index"], "identifier": plan["identifier"],
                                 "code": "write_failed",
                                 "error": "%s: %s" % (type(exc).__name__, exc)})
                break
            applied.append(plan)
        if failures:
            _rollback(inputs, plans, before, failures, "batch_write_failed")

        mismatches = []
        for plan in applied:
            ok, read_back = _verify_plan(inputs[plan["identifier"]], plan)
            if not ok:
                mismatches.append({
                    "index": plan["index"], "identifier": plan["identifier"],
                    "code": "write_verification_failed",
                    "expected": _jsonable(plan.get("expected")), "read_back": read_back,
                })
        if mismatches:
            _rollback(inputs, plans, before, mismatches, "write_verification_failed")

        diffs = []
        for plan in plans:
            identifier = plan["identifier"]
            after_value = _serialise(plan["socket_type"],
                                     _group_value(inputs[identifier], VALUE_KEY))
            after_mode = input_mode(inputs, identifier)
            after_attribute = after_mode["attribute_name"]
            diffs.append({
                "index": plan["index"],
                "identifier": identifier,
                "name": plan["name"],
                "socket_type": plan["socket_type"],
                "mode": plan["mode"],
                "before": {"value": before_value[identifier],
                           "attribute_name": before_attribute[identifier],
                           "mode": before_mode[identifier]["mode"]},
                "after": {"value": after_value, "attribute_name": after_attribute,
                          "mode": after_mode["mode"]},
                "written_keys": [write["key"] for write in plan["writes"]],
                "coerced": bool(plan.get("coerced")),
                "changed": (not _same_value(_plain(before_value[identifier]), _plain(after_value))
                            or before_attribute[identifier] != after_attribute
                            or before_mode[identifier]["mode"] != after_mode["mode"]),
                "attribute_mode_cleared": plan.get("attribute_cleared"),
            })
        return {"diffs": diffs, "before": before}

    def _set_parameters(object_name, modifier_name, specs, single):
        obj, target, group = resolve_target(object_name, modifier_name)
        inputs = instance_inputs(target)
        if inputs is None:
            raise error(
                "unsupported_blender_version",
                "this Blender build exposes no per-instance geometry-nodes inputs",
                details={"blender": getattr(getattr(bpy, "app", None), "version_string", None)},
            )
        execution = _execute(obj, target, group, inputs, specs)
        status = update_and_evaluate(obj)
        diffs = execution["diffs"]
        payload = {
            "object": obj.name,
            "modifier": target.name,
            "node_group": group.name,
            "applied": diffs,
            "applied_count": len(diffs),
            "requested_count": len(specs),
            "changed_count": len([diff for diff in diffs if diff["changed"]]),
            "all_idempotent": all(not diff["changed"] for diff in diffs),
            "depsgraph": status,
            "parameters": {
                diff["identifier"]: {"type": _short_type(diff["socket_type"]),
                                     "current": diff["after"]["value"],
                                     "attribute_name": diff["after"]["attribute_name"]}
                for diff in diffs
            },
            "warnings": list(status["warnings"]),
            "limits": {"max_batch_changes": MAX_BATCH_CHANGES},
        }
        if single:
            payload["parameter"] = diffs[0]
        return payload

    # ----------------------------------------------------------------------- #
    # commands
    # ----------------------------------------------------------------------- #
    @api.command(
        "properties_list_parameters",
        mutating=False,
        summary="enumerate one Geometry Nodes modifier's per-instance inputs",
    )
    def properties_list_parameters(object, modifier=None):
        """List every exposed parameter of one exact object+modifier instance.

        Each entry carries the stable socket ``identifier``, display ``name``, socket
        and short type, the shared node-group interface ``default``, the per-instance
        ``current`` value, the declared range/subtype/unit, whether the input is
        hidden in the modifier, whether it is linked inside the group, whether it is
        a field socket, the proven attribute-mode state, the reliably discoverable
        enum items (menu sockets) and whether this server may write it.
        """
        obj, target, group = resolve_target(object, modifier)
        inputs = instance_inputs(target)
        payload = list_input_reports(obj, target, group, inputs)
        if inputs is None:
            payload["per_instance_note"] = (
                "this Blender build exposes no modifier.properties.inputs; the listing is read-only"
            )
        return payload

    @api.command(
        "properties_get_parameter",
        mutating=False,
        summary="read one per-instance Geometry Nodes modifier input by identifier",
    )
    def properties_get_parameter(object, modifier=None, identifier=None):
        """Read one parameter of one modifier instance strictly by socket identifier."""
        return read_parameter(object, modifier, identifier)

    @api.command(
        "properties_set_parameter",
        summary="set one per-instance Geometry Nodes modifier input (value or attribute)",
    )
    def properties_set_parameter(object, modifier=None, identifier=None, value=None,
                                 mode="VALUE", attribute_name=None):
        """Set one parameter of one modifier instance, with read-back verification.

        Writes only ``modifier.properties.inputs[identifier]`` of the named object's
        modifier: the shared node-group interface default and every other object's
        instance stay untouched.  ``mode='ATTRIBUTE'`` is refused unless the live
        IDPropertyGroup proves the attribute storage (see docs/PROPERTIES.md).  A
        ``null`` value clears a datablock input and is refused for value sockets.
        """
        if not isinstance(identifier, str) or not identifier:
            raise error("invalid_argument", "identifier must be a non-empty socket identifier")
        spec = {"index": 0, "identifier": identifier, "value": value,
                "mode": _mode(mode), "attribute_name": attribute_name}
        return _set_parameters(object, modifier, [spec], single=True)

    @api.command(
        "properties_set_parameters",
        summary="set a bounded batch of per-instance Geometry Nodes modifier inputs atomically",
    )
    def properties_set_parameters(object, modifier=None, changes=None):
        """Apply up to 128 parameter changes after a full pre-flight.

        Every change is validated - identifier, socket type, value, range, mode,
        datablock reference, driver/hidden/linked state - before the first write, so
        a refused batch leaves every value and mode exactly as it was.  A failure
        during the writes, including a read-back mismatch, restores every touched
        input from a snapshot and reports that restoration honestly.
        """
        specs = _change_specs(changes)
        return _set_parameters(object, modifier, specs, single=False)

    # ----------------------------------------------------------------------- #
    # read-only helper surface for other command modules
    # ----------------------------------------------------------------------- #
    return {
        "socket_tables": {
            "value_socket_types": VALUE_SOCKET_TYPES,
            "field_socket_types": FIELD_SOCKET_TYPES,
            "pointer_socket_types": POINTER_SOCKET_TYPES,
            "read_only_socket_types": READ_ONLY_SOCKET_TYPES,
            "unsupported_socket_types": UNSUPPORTED_SOCKET_TYPES,
            "enum_socket_types": ENUM_SOCKET_TYPES,
            "attribute_name_keys": ATTRIBUTE_NAME_KEYS,
            "vector_arity": dict(VECTOR_ARITY),
            "mode_values": MODE_VALUES,
            "max_batch_changes": MAX_BATCH_CHANGES,
        },
        "resolve_target": resolve_target,
        "instance_inputs": instance_inputs,
        "instance_keys": instance_keys,
        "interface_inputs": interface_inputs,
        "attribute_mode_state": attribute_mode_state,
        "driver_reports": driver_reports,
        "enum_state": enum_state,
        "describe_input": _describe,
        "list_input_reports": list_input_reports,
        "read_parameter": read_parameter,
        "write_gate": write_gate,
        "view_layer_for": view_layer_for,
    }
