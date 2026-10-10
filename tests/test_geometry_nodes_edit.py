"""Offline unit tests for the Geometry Nodes layout / Reroute commands.

No Blender process, no socket and no ``bpy`` are involved: the bridge module is
loaded with a small in-process stub of the parts of the Blender API it uses
(``bpy.data.node_groups`` / ``bpy.data.objects``, nodes, sockets, links and the
multi-input sort id), exactly like ``test_node_frames_atomic.py`` does for native
Frames. That makes it possible to test the real command code - including the
atomic rollback paths - without pretending to have run Blender.

Live Blender 5.2.1 acceptance is done separately by the project owner; see
``docs/GEOMETRY_NODES_EDIT.md``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sys
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "blender" / "geometry_nodes_edit.py"
TOOLS_PATH = ROOT / "blender_mcp" / "tools" / "geometry_nodes_edit.py"

MULTI_ATTRS = ("multi_input_sort_id", "multi_input_socket_index")


# --------------------------------------------------------------------------- #
# stub Blender API
# --------------------------------------------------------------------------- #
class Failure(Exception):
    """Stand-in for ``api.CommandError``."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class Link:
    """NodeLink: one wire, with an optional multi-input sort id."""

    def __init__(self, tree, from_socket, to_socket):
        object.__setattr__(self, "tree", tree)
        object.__setattr__(self, "from_socket", from_socket)
        object.__setattr__(self, "to_socket", to_socket)
        object.__setattr__(self, "_sort_id", tree.next_sort_id(to_socket))

    @property
    def from_node(self):
        return self.from_socket.node

    @property
    def to_node(self):
        return self.to_socket.node

    def __getattr__(self, name):
        tree = object.__getattribute__(self, "tree")
        if name in MULTI_ATTRS:
            if tree.has_sort_attr:
                return object.__getattribute__(self, "_sort_id")
            raise AttributeError(name)
        if name == "swap_multi_input_sort_id" and tree.swap_available:
            return self._swap_sort_id
        raise AttributeError(name)

    def _swap_sort_id(self, other):
        mine = object.__getattribute__(self, "_sort_id")
        theirs = object.__getattribute__(other, "_sort_id")
        object.__setattr__(self, "_sort_id", theirs)
        object.__setattr__(other, "_sort_id", mine)

    def __setattr__(self, name, value):
        if name in MULTI_ATTRS:
            tree = object.__getattribute__(self, "tree")
            if not tree.sort_writable:
                raise AttributeError("%s is read-only in this build" % name)
            object.__setattr__(self, "_sort_id", int(value))
            return
        object.__setattr__(self, name, value)


class Socket:
    def __init__(self, node, identifier, name, in_out, socket_type="NodeSocketGeometry", multi_input=False):
        self.node = node
        self.identifier = identifier
        self.name = name
        self.in_out = in_out
        self.socket_type = socket_type
        self.type = "GEOMETRY" if "Geometry" in socket_type else "VALUE"
        self.is_multi_input = multi_input
        self.links = []
        self.default_value = None if self.type == "GEOMETRY" else 0.0

    @property
    def is_linked(self):
        return bool(self.links)


class Node:
    def __init__(self, tree, bl_idname, name):
        self.tree = tree
        self.bl_idname = bl_idname
        self.name = name
        self.bl_label = bl_idname
        self.label = ""
        self.mute = False
        self.location = [0.0, 0.0]
        self.parent = None
        self.width = 0.0
        self.height = 100.0
        self.dimensions = [0.0, 0.0]
        self.shrink = False
        self.internal_links = []
        self.inputs = []
        self.outputs = []
        self.fail_location_set = None
        self.location_sets = 0
        self._build_sockets()

    def _build_sockets(self):
        if self.bl_idname == "NodeReroute":
            self.inputs = [Socket(self, "Input", "Input", "INPUT")]
            self.outputs = [Socket(self, "Output", "Output", "OUTPUT")]
        elif self.bl_idname == "NodeFrame":
            self.inputs, self.outputs = [], []
        elif self.bl_idname == "NodeGroupInput":
            self.outputs = [Socket(self, "Geometry", "Geometry", "OUTPUT")]
        elif self.bl_idname == "NodeGroupOutput":
            self.inputs = [Socket(self, "Geometry", "Geometry", "INPUT")]
        elif self.bl_idname == "GeometryNodeJoinGeometry":
            self.inputs = [Socket(self, "Geometry", "Geometry", "INPUT", multi_input=True)]
            self.outputs = [Socket(self, "Geometry", "Geometry", "OUTPUT")]
        else:
            self.inputs = [Socket(self, "Geometry", "Geometry", "INPUT")]
            self.outputs = [Socket(self, "Geometry", "Geometry", "OUTPUT")]

    @property
    def id_data(self):
        return self.tree

    def as_pointer(self):
        return id(self)

    def __setattr__(self, name, value):
        if name == "location":
            counter = self.__dict__.get("location_sets", 0) + 1
            object.__setattr__(self, "location_sets", counter)
            fail = self.__dict__.get("fail_location_set")
            if fail is not None and counter >= fail:
                object.__setattr__(self, "fail_location_set", None)
                raise RuntimeError("injected node.location failure")
            object.__setattr__(self, "location", [float(value[0]), float(value[1])])
            return
        object.__setattr__(self, name, value)

    def __repr__(self):
        return "Node(%s)" % self.name


class NodeCollection:
    def __init__(self, tree):
        self.tree = tree
        self._items = []
        self.fail_after = None
        self.new_calls = 0

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def get(self, name):
        return next((node for node in self._items if node.name == name), None)

    def append(self, node):
        self._items.append(node)

    def new(self, bl_idname):
        self.new_calls += 1
        if self.fail_after is not None and self.new_calls > self.fail_after:
            raise RuntimeError("injected nodes.new failure")
        name, index = self.base_name(bl_idname), 0
        while self.get(name) is not None:
            index += 1
            name = "%s.%03d" % (self.base_name(bl_idname), index)
        node = Node(self.tree, bl_idname, name)
        self._items.append(node)
        return node

    @staticmethod
    def base_name(bl_idname):
        """Blender's default node names for the types this suite uses."""
        return {
            "NodeReroute": "Reroute",
            "NodeFrame": "Frame",
            "NodeGroupInput": "Group Input",
            "NodeGroupOutput": "Group Output",
        }.get(bl_idname, bl_idname)

    def fail_after_calls(self, count):
        """Fail the ``count``-th ``new()`` call from now on."""
        self.fail_after = self.new_calls + count

    def remove(self, node):
        for link in [item for item in self.tree.links if item.from_node is node or item.to_node is node]:
            self.tree.links.remove(link)
        self._items.remove(node)


class LinkCollection:
    def __init__(self, tree):
        self.tree = tree
        self._items = []
        self.fail_after = None
        self.new_calls = 0
        self.sabotage = None

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def append_duplicate(self, from_socket, to_socket):
        """Fabricate a second identical link (for the ambiguity check)."""
        link = Link(self.tree, from_socket, to_socket)
        self._items.append(link)
        from_socket.links.append(link)
        to_socket.links.append(link)
        return link

    def fail_after_calls(self, count):
        """Fail the ``count``-th ``new()`` call from now on."""
        self.fail_after = self.new_calls + count - 1

    def sabotage_at_call(self, offset, from_socket):
        """Miswire the ``offset``-th ``new()`` call from now on (once)."""
        self.sabotage = {"at": self.new_calls + offset, "from_socket": from_socket}

    def new(self, from_socket, to_socket):
        self.new_calls += 1
        if self.fail_after is not None and self.new_calls > self.fail_after:
            self.fail_after = None  # one-shot: a transient write failure
            raise RuntimeError("injected links.new failure")
        if from_socket.in_out == "INPUT" and to_socket.in_out == "OUTPUT":
            from_socket, to_socket = to_socket, from_socket
        if from_socket.in_out != "OUTPUT" or to_socket.in_out != "INPUT":
            raise RuntimeError("cannot link %s to %s" % (from_socket.in_out, to_socket.in_out))
        if self.sabotage is not None and self.new_calls == self.sabotage["at"]:
            from_socket = self.sabotage["from_socket"]
            self.sabotage = None
        if not to_socket.is_multi_input:
            for existing in list(to_socket.links):
                self.remove(existing)
        if any(link.from_socket is from_socket and link.to_socket is to_socket for link in self._items):
            raise RuntimeError("a link between these sockets already exists")
        link = Link(self.tree, from_socket, to_socket)
        self._items.append(link)
        from_socket.links.append(link)
        to_socket.links.append(link)
        return link

    def remove(self, link):
        self._items.remove(link)
        link.from_socket.links.remove(link)
        link.to_socket.links.remove(link)


class InterfaceItem:
    def __init__(self, name, identifier, in_out, socket_type="NodeSocketGeometry", item_type="SOCKET"):
        self.name = name
        self.identifier = identifier
        self.in_out = in_out
        self.socket_type = socket_type
        self.item_type = item_type
        self.description = ""
        self.parent = None


class Interface:
    def __init__(self, items):
        self.items_tree = items


class Tree:
    def __init__(self, name, bl_idname="GeometryNodeTree", library=None, override_library=None):
        self.name = name
        self.bl_idname = bl_idname
        self.bl_rna = types.SimpleNamespace(identifier=bl_idname)
        self.library = library
        self.override_library = override_library
        self.users = 1
        self.nodes = NodeCollection(self)
        self.links = LinkCollection(self)
        self.interface = Interface([
            InterfaceItem("Geometry", "Geometry", "INPUT"),
            InterfaceItem("Geometry", "Geometry", "OUTPUT"),
        ])
        self.update_tag_calls = 0
        self.has_sort_attr = True
        self.sort_writable = True
        self.swap_available = False
        self.link_class = Link

    def update_tag(self):
        self.update_tag_calls += 1

    def next_sort_id(self, socket):
        if not socket.is_multi_input:
            return 0
        ids = [object.__getattribute__(link, "_sort_id") for link in socket.links]
        return (max(ids) + 1) if ids else 0

    def add_node(self, bl_idname, name=None, location=(0.0, 0.0)):
        if name is None:
            node = self.nodes.new(bl_idname)
        else:
            node = Node(self, bl_idname, name)
            self.nodes.append(node)
        node.location = [location[0], location[1]]
        return node

    def connect(self, from_node, from_socket, to_node, to_socket):
        source = next(s for s in from_node.outputs if s.identifier == from_socket)
        target = next(s for s in to_node.inputs if s.identifier == to_socket)
        return self.links.new(source, target)

    def snapshot(self):
        return {
            # node collection order is cosmetic in Blender, so compare by name
            "nodes": sorted((node.name, node.bl_idname, tuple(node.location)) for node in self.nodes),
            "links": [
                (link.from_node.name, link.from_socket.identifier, link.to_node.name,
                 link.to_socket.identifier, object.__getattribute__(link, "_sort_id"))
                for link in self.links
            ],
        }


class Modifier:
    def __init__(self, name, node_group=None, type="NODES"):
        self.name = name
        self.node_group = node_group
        self.type = type


class Object:
    def __init__(self, name, modifiers=None, type="MESH"):
        self.name = name
        self.type = type
        self.modifiers = modifiers or []


# --------------------------------------------------------------------------- #
# module loading
# --------------------------------------------------------------------------- #
def load_bridge_module(data):
    sys.modules["bpy"] = types.SimpleNamespace(data=data)
    name = "gn_edit_under_test_%d" % id(data)
    spec = importlib.util.spec_from_file_location(name, BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness(unittest.TestCase):
    """Common fixture: one GeometryNodeTree plus the four binding objects."""

    def setUp(self):
        self.data = types.SimpleNamespace(node_groups=[], objects=[])
        self.module = load_bridge_module(self.data)
        self.tree = Tree("Group")
        self.data.node_groups.append(self.tree)
        self.input = self.tree.add_node("NodeGroupInput", "Input", (-400.0, 0.0))
        self.setpos = self.tree.add_node("GeometryNodeSetPosition", "SetPos", (0.0, 0.0))
        self.join = self.tree.add_node("GeometryNodeJoinGeometry", "Join", (300.0, 0.0))
        self.output = self.tree.add_node("NodeGroupOutput", "Output", (600.0, 0.0))
        self.frame = self.tree.add_node("NodeFrame", "FrameA", (-450.0, 150.0))
        self.setpos.parent = self.frame
        self.setpos.location = [0.0, 0.0]
        self.link_in = self.tree.connect(self.input, "Geometry", self.join, "Geometry")
        self.link_pos = self.tree.connect(self.setpos, "Geometry", self.join, "Geometry")
        self.link_out = self.tree.connect(self.join, "Geometry", self.output, "Geometry")
        self.flush_calls = []
        self.commands = {}
        self.module.register_commands(self.api())

    def api(self, with_flush=True):
        commands = self.commands if hasattr(self, "commands") else {}

        def command(name, mutating=True, summary=""):
            def decorator(func):
                commands[name] = func
                return func
            return decorator

        api = types.SimpleNamespace(CommandError=Failure, command=command)
        if with_flush:
            def flush_group_users(tree):
                self.flush_calls.append(tree.name)
                return len(self.data.objects)
            api.flush_group_users = flush_group_users
        return api

    # -- helpers ---------------------------------------------------------- #
    def call(self, name, **params):
        return self.commands[name](**params)

    def error(self, code, name, **params):
        with self.assertRaises(Failure) as caught:
            self.call(name, **params)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def reroute_names(self):
        return sorted(node.name for node in self.tree.nodes if node.bl_idname == "NodeReroute")

    def link_rows(self):
        return [
            (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
            for link in self.tree.links
        ]

    def multi_order(self, node_name="Join", socket="Geometry"):
        rows = [
            link for link in self.tree.links
            if link.to_node.name == node_name and link.to_socket.identifier == socket
        ]
        rows.sort(key=lambda link: object.__getattribute__(link, "_sort_id"))
        return [(link.from_node.name, link.from_socket.identifier) for link in rows]


class LayoutTests(Harness):
    def test_layout_reports_the_whole_graph(self):
        payload = self.call("geometry_nodes_layout", group="Group")
        self.assertEqual(payload["group"], "Group")
        self.assertEqual(payload["tree_type"], "GeometryNodeTree")
        self.assertTrue(payload["is_geometry_tree"])
        self.assertIsNone(payload["object"])
        self.assertEqual(payload["counts"]["nodes"], 5)
        self.assertEqual(payload["counts"]["links"], 3)
        self.assertEqual(payload["counts"]["frames"], 1)
        self.assertEqual(payload["counts"]["reroutes"], 0)
        self.assertEqual(payload["counts"]["multi_input_sockets"], 1)
        by_name = {node["name"]: node for node in payload["nodes"]}
        self.assertEqual(by_name["SetPos"]["parent"], "FrameA")
        # canvas-local vs absolute coordinates of a Frame child
        self.assertEqual(by_name["SetPos"]["location"], [0.0, 0.0])
        self.assertEqual(by_name["SetPos"]["location_absolute"], [-450.0, 150.0])
        self.assertEqual(by_name["Input"]["location"], [-400.0, 0.0])
        self.assertFalse(by_name["SetPos"]["layout_metrics_reliable"])
        self.assertEqual(by_name["Join"]["inputs"][0]["is_multi_input"], True)
        self.assertEqual(by_name["Join"]["inputs"][0]["link_count"], 2)
        self.assertEqual(payload["frames"][0]["name"], "FrameA")
        self.assertEqual(payload["frames"][0]["children"], ["SetPos"])
        join_links = [link for link in payload["links"] if link["to_node"] == "Join"]
        self.assertEqual(sorted(link["multi_input_sort_id"] for link in join_links), [0, 1])
        self.assertEqual(
            sorted(link["multi_input_order"] for link in join_links), [0, 1]
        )
        self.assertTrue(all(link["multi_input"] for link in join_links))
        self.assertEqual(
            payload["multi_input_sockets"][0]["order"], ["Input.Geometry", "SetPos.Geometry"]
        )
        self.assertEqual(payload["signature"]["edges"], [
            ["Input", "Geometry", "Join", "Geometry"],
            ["Join", "Geometry", "Output", "Geometry"],
            ["SetPos", "Geometry", "Join", "Geometry"],
        ])
        self.assertEqual(len(payload["interface"]["inputs"]), 1)
        self.assertEqual(payload["interface"]["inputs"][0]["identifier"], "Geometry")
        self.assertEqual(payload["modifier_users"], [])
        self.assertIn("never writes", " ".join(payload["notes"]))
        self.assertEqual(self.tree.update_tag_calls, 0)

    def test_layout_reports_modifier_users_and_object(self):
        modifier = Modifier("GN", self.tree)
        self.data.objects.append(Object("Cube", [modifier]))
        payload = self.call("geometry_nodes_layout", group="Group", object="Cube", modifier="GN")
        self.assertEqual(payload["object"], "Cube")
        self.assertEqual(payload["modifier"], "GN")
        self.assertEqual(payload["modifier_users"], [{"object": "Cube", "modifier": "GN"}])

    def test_layout_refuses_group_modifier_mismatch(self):
        other = Tree("Other")
        self.data.node_groups.append(other)
        self.data.objects.append(Object("Cube", [Modifier("GN", other)]))
        self.error("group_modifier_mismatch", "geometry_nodes_layout", group="Group", object="Cube", modifier="GN")

    def test_layout_refuses_ambiguous_modifier(self):
        self.data.objects.append(Object("Cube", [Modifier("A", self.tree), Modifier("B", self.tree)]))
        self.error("ambiguous_modifier", "geometry_nodes_layout", group="Group", object="Cube")
        self.error("modifier_not_found", "geometry_nodes_layout", group="Group", object="Cube", modifier="C")

    def test_layout_refuses_object_without_geometry_nodes_modifier(self):
        self.data.objects.append(Object("Cube", [Modifier("Solidify", self.tree, type="SOLIDIFY")]))
        self.error("modifier_not_found", "geometry_nodes_layout", group="Group", object="Cube")

    def test_layout_refuses_modifier_without_object(self):
        self.error("invalid_argument", "geometry_nodes_layout", group="Group", modifier="GN")

    def test_layout_refuses_non_geometry_tree_and_ambiguity(self):
        self.data.node_groups.append(Tree("ShaderTree", bl_idname="ShaderNodeTree"))
        self.error("unsupported_tree", "geometry_nodes_layout", group="ShaderTree")
        self.data.node_groups.append(Tree("Group"))
        self.error("ambiguous_name", "geometry_nodes_layout", group="Group")
        self.error("node_group_not_found", "geometry_nodes_layout", group="Missing")
        self.error("invalid_name", "geometry_nodes_layout", group="")

    def test_layout_handles_pre_existing_reroute_and_socket_defaults(self):
        reroute = self.tree.add_node("NodeReroute", location=(200.0, -50.0))
        self.tree.links.remove(self.link_out)
        self.tree.connect(self.join, "Geometry", reroute, "Input")
        self.tree.links.new(reroute.outputs[0], self.output.inputs[0])
        payload = self.call("geometry_nodes_layout", group="Group")
        self.assertEqual(payload["counts"]["reroutes"], 1)
        self.assertEqual(payload["reroutes"][0]["name"], "Reroute")
        self.assertEqual(payload["reroutes"][0]["inputs"], 1)
        self.assertEqual(payload["reroutes"][0]["outputs"], 1)
        self.assertEqual(payload["reroutes"][0]["outgoing"], 1)
        # reroutes are resolved away in the semantic signature
        self.assertIn(["Join", "Geometry", "Output", "Geometry"], payload["signature"]["edges"])
        self.assertEqual(payload["signature"]["dangling_reroute_inputs"], [])
        self.assertFalse(payload["signature"]["reroute_cycle"])
        flags = {link["from_node"]: link["is_reroute_link"] for link in payload["links"]}
        self.assertTrue(flags["Reroute"])


class MoveTests(Harness):
    def test_move_nodes_only_touches_named_nodes(self):
        payload = self.call("geometry_nodes_move_nodes", group="Group",
                            positions=[{"node": "Join", "x": 320, "y": 40}])
        self.assertEqual(payload["moved_count"], 1)
        self.assertEqual([float(v) for v in self.join.location], [320.0, 40.0])
        self.assertEqual([float(v) for v in self.setpos.location], [0.0, 0.0])
        self.assertTrue(payload["links_unchanged"])
        self.assertTrue(payload["validation"]["equivalent"])
        self.assertTrue(payload["validation"]["node_locations_applied"])
        self.assertEqual(payload["before"]["link_count"], 3)
        self.assertEqual(payload["after"]["link_count"], 3)
        self.assertEqual(self.flush_calls, ["Group"])

    def test_move_nodes_absolute_subtracts_parent_frame(self):
        payload = self.call("geometry_nodes_move_nodes", group="Group", absolute=True,
                            positions=[{"node": "SetPos", "x": -400.0, "y": 200.0}])
        self.assertEqual([float(v) for v in self.setpos.location], [50.0, 50.0])
        self.assertEqual(payload["moved"][0]["after_absolute"], [-400.0, 200.0])
        self.assertTrue(payload["validation"]["node_locations_applied"])

    def test_move_nodes_refuses_invalid_input_without_writing(self):
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Missing", "x": 0, "y": 0}])
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Join", "x": 0, "y": 0}, {"node": "Join", "x": 1, "y": 1}])
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Join", "x": float("nan"), "y": 0}])
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Join", "x": "0", "y": 0}])
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group", positions=[])
        self.error("invalid_positions", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Join", "x": 1e9, "y": 0}])
        self.assertEqual([float(v) for v in self.join.location], [300.0, 0.0])

    def test_move_nodes_rolls_back_on_write_failure(self):
        self.join.fail_location_set = 1
        before = self.tree.snapshot()
        exc = self.error("move_nodes_failed", "geometry_nodes_move_nodes", group="Group",
                         positions=[{"node": "Join", "x": 10, "y": 10}, {"node": "Output", "x": 20, "y": 20}])
        self.assertEqual(exc.details.get("rolled_back"), True)
        self.assertEqual(self.tree.snapshot(), before)

    def test_move_nodes_refuses_linked_tree(self):
        self.tree.library = types.SimpleNamespace(name="lib")
        self.error("read_only_tree", "geometry_nodes_move_nodes", group="Group",
                   positions=[{"node": "Join", "x": 1, "y": 1}])

    def test_move_nodes_falls_back_to_update_tag_without_flush_helper(self):
        self.commands.clear()
        self.module.register_commands(self.api(with_flush=False))
        payload = self.call("geometry_nodes_move_nodes", group="Group",
                            positions=[{"node": "Join", "x": 5, "y": 5}])
        self.assertEqual(payload["tagged"]["method"], "tree.update_tag")
        self.assertGreater(self.tree.update_tag_calls, 0)


class RerouteInsertTests(Harness):
    def spec(self, link):
        return {
            "from_node": link.from_node.name,
            "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name,
            "to_socket": link.to_socket.identifier,
        }

    def test_single_link_insert_is_semantically_identical(self):
        payload = self.call("geometry_nodes_reroute_insert", group="Group",
                            links=[self.spec(self.link_in)])
        self.assertEqual(payload["created_count"], 1)
        self.assertEqual(self.reroute_names(), ["Reroute"])
        self.assertEqual(payload["reroute_sockets"], {"inputs": 1, "outputs": 1})
        self.assertEqual(payload["positions_source"], "midpoint")
        self.assertEqual(payload["created"][0]["location"], [-50.0, 0.0])
        self.assertEqual(payload["created"][0]["source"], {"node": "Input", "socket": "Geometry"})
        self.assertTrue(payload["validation"]["equivalent"])
        self.assertEqual(payload["before"]["link_count"], 3)
        self.assertEqual(payload["after"]["link_count"], 4)
        self.assertIn(
            ("Reroute", "Output", "Join", "Geometry"),
            [(row["from_node"], row["from_socket"], row["to_node"], row["to_socket"]) for row in payload["after"]["links"]],
        )
        # the reroute took over the first slot; its resolved source is still Input
        self.assertEqual(self.multi_order(), [("Reroute", "Output"), ("SetPos", "Geometry")])
        self.assertEqual(payload["validation"]["multi_input_order_equal"], True)
        self.assertEqual(payload["after"]["multi_input_orders"]["Join.Geometry"],
                         ["Input.Geometry", "SetPos.Geometry"])
        self.assertEqual(payload["multi_input_order"][0]["method"], "write:multi_input_sort_id")
        self.assertEqual(self.flush_calls, ["Group"])

    def test_two_sources_into_one_multi_input_get_independent_reroutes(self):
        payload = self.call(
            "geometry_nodes_reroute_insert", group="Group",
            links=[self.spec(self.link_in), self.spec(self.link_pos)],
            points=[[-100.0, 60.0], [-100.0, -60.0]],
        )
        self.assertEqual(payload["created_count"], 2)
        self.assertEqual(payload["positions_source"], "explicit")
        self.assertEqual(self.reroute_names(), ["Reroute", "Reroute.001"])
        for item in payload["created"]:
            self.assertEqual(len(item["targets"]), 1)
            self.assertEqual(item["targets"][0]["node"], "Join")
            self.assertTrue(item["targets"][0]["multi_input"])
        self.assertEqual(self.multi_order(), [("Reroute", "Output"), ("Reroute.001", "Output")])
        self.assertEqual(payload["validation"]["multi_input_order_equal"], True)
        self.assertTrue(payload["validation"]["equivalent"])

    def test_shared_branch_fans_one_source_out_to_many_targets(self):
        branch = self.tree.add_node("GeometryNodeTransform", "Branch", (300.0, -200.0))
        branch.inputs[0].is_multi_input = False
        self.tree.connect(self.setpos, "Geometry", branch, "Geometry")
        payload = self.call(
            "geometry_nodes_reroute_insert", group="Group",
            links=[self.spec(self.link_pos),
                   {"from_node": "SetPos", "from_socket": "Geometry", "to_node": "Branch", "to_socket": "Geometry"}],
            shared_branch=True,
        )
        self.assertEqual(payload["created_count"], 1)
        self.assertEqual(len(payload["created"][0]["targets"]), 2)
        self.assertEqual(
            sorted(target["node"] for target in payload["created"][0]["targets"]), ["Branch", "Join"]
        )
        self.assertTrue(payload["validation"]["equivalent"])
        self.assertEqual(payload["before"]["link_count"], 4)
        self.assertEqual(payload["after"]["link_count"], 5)

    def test_shared_branch_never_merges_different_sources(self):
        payload = self.call(
            "geometry_nodes_reroute_insert", group="Group",
            links=[self.spec(self.link_in), self.spec(self.link_pos)],
            shared_branch=True,
        )
        self.assertEqual(payload["created_count"], 2)
        self.assertEqual(payload["created"][0]["source"]["node"], "Input")
        self.assertEqual(payload["created"][1]["source"]["node"], "SetPos")
        self.assertTrue(payload["validation"]["equivalent"])

    def test_shared_branch_refuses_duplicate_target_of_one_source(self):
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in), self.spec(self.link_in)], shared_branch=True)
        self.assertEqual(self.reroute_names(), [])

    def test_insert_refuses_unknown_duplicate_and_ambiguous_links(self):
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[{"from_node": "Input", "from_socket": "Geometry", "to_node": "Output",
                           "to_socket": "Geometry"}])
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in), self.spec(self.link_in)])
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[{"from_node": "Input", "from_socket": "Geometry", "to_node": "Join"}])
        # a fabricated second identical link makes the key ambiguous
        self.tree.links.append_duplicate(self.input.outputs[0], self.join.inputs[0])
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in)])
        self.assertEqual(self.reroute_names(), [])

    def test_insert_refuses_bad_points_without_writing(self):
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in)], points=[[0.0, 0.0], [1.0, 1.0]])
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in)], points=[[0.0, float("inf")]])
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in)], points=[[0.0, 0.0, 0.0]])
        self.assertEqual(self.reroute_names(), [])

    def test_insert_refuses_too_many_links(self):
        too_many = [self.spec(self.link_in)] * 129
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group", links=too_many)

    def test_insert_asserts_multi_input_sort_id_when_given(self):
        spec = dict(self.spec(self.link_pos), multi_input_sort_id=1)
        payload = self.call("geometry_nodes_reroute_insert", group="Group", links=[spec])
        self.assertEqual(payload["created_count"], 1)
        self.assertEqual(payload["created"][0]["targets"][0]["multi_input_sort_id"], 1)
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[dict(self.spec(self.link_pos), multi_input_sort_id=7)])

    def test_insert_refuses_when_multi_input_order_api_is_missing(self):
        self.tree.has_sort_attr = False
        before = self.tree.snapshot()
        self.error("multi_input_order_api_unavailable", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_pos)])
        self.assertEqual(self.tree.snapshot(), before)
        self.assertEqual(self.reroute_names(), [])

    def test_insert_restores_order_by_recreating_links(self):
        self.tree.sort_writable = False
        # inserting into the FIRST multi-input link forces a real reorder
        payload = self.call("geometry_nodes_reroute_insert", group="Group",
                            links=[self.spec(self.link_in)])
        self.assertEqual(payload["multi_input_order_method"], ["recreate_links"])
        self.assertEqual(self.multi_order(), [("Reroute", "Output"), ("SetPos", "Geometry")])
        self.assertTrue(payload["validation"]["equivalent"])
        self.assertTrue(payload["validation"]["multi_input_order_equal"])

    def test_insert_restores_order_by_swapping_sort_ids(self):
        self.tree.sort_writable = False
        self.tree.swap_available = True
        payload = self.call("geometry_nodes_reroute_insert", group="Group",
                            links=[self.spec(self.link_in)])
        self.assertEqual(payload["multi_input_order_method"], ["swap_multi_input_sort_id"])
        self.assertEqual(self.multi_order(), [("Reroute", "Output"), ("SetPos", "Geometry")])
        self.assertTrue(payload["validation"]["equivalent"])

    def test_insert_rolls_back_when_wiring_fails(self):
        before = self.tree.snapshot()
        self.tree.links.fail_after_calls(1)
        exc = self.error("link_failed", "geometry_nodes_reroute_insert", group="Group",
                         links=[self.spec(self.link_in), self.spec(self.link_pos)])
        self.assertEqual(exc.details.get("rolled_back"), True)
        self.assertEqual(self.tree.snapshot(), before)
        self.assertEqual(self.reroute_names(), [])

    def test_insert_rolls_back_when_verification_fails(self):
        before = self.tree.snapshot()
        # Blender "miswires" the second created link to another source socket:
        # the semantic signature no longer matches and the edit must be undone.
        self.tree.links.sabotage_at_call(2, self.join.outputs[0])
        exc = self.error("verification_failed", "geometry_nodes_reroute_insert", group="Group",
                         links=[self.spec(self.link_in)])
        self.assertEqual(exc.details.get("rolled_back"), True)
        self.assertFalse(exc.details["validation"]["equivalent"])
        self.assertEqual(self.tree.snapshot(), before)
        self.assertEqual(self.reroute_names(), [])

    def test_insert_refuses_non_bool_shared_branch(self):
        self.error("invalid_links", "geometry_nodes_reroute_insert", group="Group",
                   links=[self.spec(self.link_in)], shared_branch="yes")


class RerouteMoveTests(Harness):
    def setUp(self):
        super().setUp()
        self.reroute = self.tree.add_node("NodeReroute", location=(150.0, 25.0))
        self.tree.links.remove(self.link_out)
        self.tree.connect(self.join, "Geometry", self.reroute, "Input")
        self.tree.links.new(self.reroute.outputs[0], self.output.inputs[0])

    def test_move_moves_only_reroutes(self):
        payload = self.call("geometry_nodes_reroute_move", group="Group",
                            positions=[{"node": "Reroute", "x": 175.0, "y": -10.0}])
        self.assertEqual([float(v) for v in self.reroute.location], [175.0, -10.0])
        self.assertEqual([float(v) for v in self.join.location], [300.0, 0.0])
        self.assertTrue(payload["validation"]["equivalent"])

    def test_move_refuses_real_nodes(self):
        self.error("invalid_positions", "geometry_nodes_reroute_move", group="Group",
                   positions=[{"node": "Join", "x": 0, "y": 0}])
        self.assertEqual([float(v) for v in self.join.location], [300.0, 0.0])

    def test_move_rolls_back_on_failure(self):
        self.reroute.fail_location_set = 1
        before = self.tree.snapshot()
        exc = self.error("reroute_move_failed", "geometry_nodes_reroute_move", group="Group",
                         positions=[{"node": "Reroute", "x": 0.0, "y": 0.0}])
        self.assertEqual(exc.details.get("rolled_back"), True)
        self.assertEqual(self.tree.snapshot(), before)


class RerouteDeleteTests(Harness):
    def add_reroute_between(self, from_node, from_socket, to_node, to_socket, location=(150.0, 0.0)):
        """Replace one link with source -> Reroute -> target and return the reroute."""
        reroute = self.tree.add_node("NodeReroute", location=location)
        source = next(s for s in from_node.outputs if s.identifier == from_socket)
        target = next(s for s in to_node.inputs if s.identifier == to_socket)
        existing = next(
            link for link in list(self.tree.links)
            if link.from_socket is source and link.to_socket is target
        )
        self.tree.links.remove(existing)
        self.tree.links.new(source, reroute.inputs[0])
        self.tree.links.new(reroute.outputs[0], target)
        return reroute

    def test_delete_reconnects_source_and_keeps_multi_input_order(self):
        reroute = self.add_reroute_between(self.setpos, "Geometry", self.join, "Geometry")
        payload = self.call("geometry_nodes_reroute_delete", group="Group", nodes=["Reroute"])
        self.assertEqual(payload["deleted"], ["Reroute"])
        self.assertEqual(self.reroute_names(), [])
        self.assertEqual(self.multi_order(), [("Input", "Geometry"), ("SetPos", "Geometry")])
        self.assertTrue(payload["validation"]["equivalent"])
        self.assertTrue(payload["validation"]["multi_input_order_equal"])
        self.assertEqual(payload["reconnected"][0]["source"], {"node": "SetPos", "socket": "Geometry"})
        self.assertEqual(payload["before"]["link_count"], 4)
        self.assertEqual(payload["after"]["link_count"], 3)
        self.assertTrue(reroute.name not in [node.name for node in self.tree.nodes])

    def test_delete_handles_a_branch(self):
        reroute = self.add_reroute_between(self.join, "Geometry", self.output, "Geometry")
        branch = self.tree.add_node("GeometryNodeTransform", "Branch", (300.0, -200.0))
        self.tree.links.new(reroute.outputs[0], branch.inputs[0])
        payload = self.call("geometry_nodes_reroute_delete", group="Group", nodes=["Reroute"])
        self.assertEqual(len(payload["reconnected"]), 2)
        self.assertEqual(
            sorted(entry["target"]["node"] for entry in payload["reconnected"]), ["Branch", "Output"]
        )
        self.assertEqual(sorted(self.link_rows()), [
            ("Input", "Geometry", "Join", "Geometry"),
            ("Join", "Geometry", "Branch", "Geometry"),
            ("Join", "Geometry", "Output", "Geometry"),
            ("SetPos", "Geometry", "Join", "Geometry"),
        ])
        self.assertTrue(payload["validation"]["equivalent"])

    def test_delete_handles_a_reroute_chain(self):
        first = self.add_reroute_between(self.setpos, "Geometry", self.join, "Geometry")
        second = self.tree.add_node("NodeReroute", location=(220.0, 0.0))
        # rewire: SetPos -> Reroute -> Reroute.001 -> Join
        self.tree.links.remove(next(
            link for link in list(self.tree.links)
            if link.from_socket is first.outputs[0] and link.to_socket is self.join.inputs[0]
        ))
        self.tree.links.new(first.outputs[0], second.inputs[0])
        self.tree.links.new(second.outputs[0], self.join.inputs[0])
        payload = self.call("geometry_nodes_reroute_delete", group="Group",
                            nodes=["Reroute", "Reroute.001"])
        self.assertEqual(payload["deleted_count"], 2)
        self.assertEqual(self.reroute_names(), [])
        self.assertEqual(self.multi_order(), [("Input", "Geometry"), ("SetPos", "Geometry")])
        self.assertTrue(payload["validation"]["equivalent"])

    def test_delete_refuses_a_reroute_without_input(self):
        orphan = self.tree.add_node("NodeReroute", location=(0.0, -300.0))
        before = self.tree.snapshot()
        exc = self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group", nodes=[orphan.name])
        self.assertIn("reroute_input_missing", [problem["code"] for problem in exc.details["problems"]])
        self.assertEqual(self.tree.snapshot(), before)
        self.assertIsNotNone(self.tree.nodes.get(orphan.name))

    def test_delete_refuses_to_merge_two_independent_sources(self):
        self.add_reroute_between(self.setpos, "Geometry", self.join, "Geometry")
        second = self.tree.add_node("NodeReroute", location=(150.0, -120.0))
        self.tree.links.new(self.setpos.outputs[0], second.inputs[0])
        self.tree.links.new(second.outputs[0], self.join.inputs[0])
        before = self.tree.snapshot()
        exc = self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group",
                         nodes=["Reroute", "Reroute.001"])
        self.assertIn("delete_would_merge_sources", [problem["code"] for problem in exc.details["problems"]])
        self.assertEqual(self.tree.snapshot(), before)

    def test_delete_refuses_unknown_and_wrong_node_type(self):
        self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group", nodes=["Missing"])
        self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group", nodes=["Join"])
        self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group", nodes=[])
        self.error("invalid_nodes", "geometry_nodes_reroute_delete", group="Group", nodes=[1])

    def test_delete_reports_dead_end_without_dropping_the_input(self):
        reroute = self.tree.add_node("NodeReroute", location=(150.0, -200.0))
        self.tree.links.new(self.setpos.outputs[0], reroute.inputs[0])
        payload = self.call("geometry_nodes_reroute_delete", group="Group", nodes=["Reroute"])
        self.assertEqual(payload["dead_ends"], ["Reroute"])
        self.assertIn("dead-end", " ".join(payload["warnings"]))
        self.assertEqual(sorted(self.link_rows()), [
            ("Input", "Geometry", "Join", "Geometry"),
            ("Join", "Geometry", "Output", "Geometry"),
            ("SetPos", "Geometry", "Join", "Geometry"),
        ])
        self.assertTrue(payload["validation"]["equivalent"])

    def test_delete_rolls_back_on_failure(self):
        self.add_reroute_between(self.setpos, "Geometry", self.join, "Geometry")
        before = self.tree.snapshot()
        self.tree.links.fail_after_calls(1)
        exc = self.error("link_failed", "geometry_nodes_reroute_delete", group="Group",
                         nodes=["Reroute"])
        self.assertEqual(exc.details.get("rolled_back"), True, exc.details)
        self.assertEqual(self.tree.snapshot(), before)
        self.assertEqual(self.reroute_names(), ["Reroute"])
        self.assertEqual(self.multi_order(), [("Input", "Geometry"), ("Reroute", "Output")])

    def test_delete_rolls_back_when_verification_fails(self):
        self.add_reroute_between(self.setpos, "Geometry", self.join, "Geometry")
        before = self.tree.snapshot()
        self.tree.links.sabotage_at_call(1, self.join.outputs[0])
        exc = self.error("verification_failed", "geometry_nodes_reroute_delete", group="Group",
                         nodes=["Reroute"])
        self.assertEqual(exc.details.get("rolled_back"), True, exc.details)
        self.assertEqual(self.tree.snapshot(), before)
        self.assertEqual(self.reroute_names(), ["Reroute"])


class PureLogicTests(unittest.TestCase):
    """The planners and the semantic signature are pure and tested directly."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_bridge_module(types.SimpleNamespace(node_groups=[], objects=[]))

    def graph(self, nodes, links):
        return {
            "nodes": [{"name": name, "bl_idname": kind, "location": [0.0, 0.0]} for name, kind in nodes],
            "links": [
                {
                    "from_node": row[0], "from_socket": row[1], "to_node": row[2], "to_socket": row[3],
                    "multi_input_sort_id": row[4] if len(row) > 4 else 0,
                    "index": index,
                    "to_multi_input": row[5] if len(row) > 5 else False,
                }
                for index, row in enumerate(links)
            ],
        }

    def test_allocate_names_is_bounded_and_never_reuses_a_name(self):
        module = self.module
        self.assertEqual(module.allocate_names(set(), 3), ["Reroute", "Reroute.001", "Reroute.002"])
        self.assertEqual(module.allocate_names({"Reroute", "Reroute.001"}, 2), ["Reroute.002", "Reroute.003"])
        self.assertEqual(module.allocate_names(set(), 0), [])

    def test_signature_ignores_reroutes_but_not_order(self):
        module = self.module
        direct = self.graph(
            [("In", "NodeGroupInput"), ("Join", "GeometryNodeJoinGeometry"), ("Out", "NodeGroupOutput")],
            [("In", "Geometry", "Join", "Geometry", 0, True), ("Join", "Geometry", "Out", "Geometry", 0, False)],
        )
        rerouted = self.graph(
            [("In", "NodeGroupInput"), ("R", "NodeReroute"),
             ("Join", "GeometryNodeJoinGeometry"), ("Out", "NodeGroupOutput")],
            [("In", "Geometry", "R", "Input", 0, False), ("R", "Output", "Join", "Geometry", 0, True),
             ("Join", "Geometry", "Out", "Geometry", 0, False)],
        )
        self.assertTrue(module.compare_signatures(
            module.semantic_signature(direct), module.semantic_signature(rerouted))["equivalent"])

        two_sources = self.graph(
            [("A", "GeometryNodeTransform"), ("B", "GeometryNodeTransform"),
             ("Join", "GeometryNodeJoinGeometry")],
            [("A", "Geometry", "Join", "Geometry", 0, True), ("B", "Geometry", "Join", "Geometry", 1, True)],
        )
        swapped = self.graph(
            [("A", "GeometryNodeTransform"), ("B", "GeometryNodeTransform"),
             ("Join", "GeometryNodeJoinGeometry")],
            [("B", "Geometry", "Join", "Geometry", 0, True), ("A", "Geometry", "Join", "Geometry", 1, True)],
        )
        result = module.compare_signatures(
            module.semantic_signature(two_sources), module.semantic_signature(swapped))
        self.assertFalse(result["equivalent"])
        self.assertFalse(result["multi_input_order_equal"])

    def test_signature_detects_dangling_and_cyclic_reroutes(self):
        module = self.module
        dangling = self.graph(
            [("R", "NodeReroute"), ("Out", "NodeGroupOutput")],
            [("R", "Output", "Out", "Geometry", 0, False)],
        )
        signature = module.semantic_signature(dangling)
        self.assertEqual(signature["dangling_reroute_inputs"], ["R"])
        self.assertFalse(signature["reroute_cycle"])
        cyclic = self.graph(
            [("R", "NodeReroute"), ("S", "NodeReroute")],
            [("R", "Output", "S", "Input", 0, False), ("S", "Output", "R", "Input", 0, False)],
        )
        self.assertTrue(module.semantic_signature(cyclic)["reroute_cycle"])

    def test_plan_insert_groups_shared_source_but_not_fan_in(self):
        module = self.module
        graph = self.graph(
            [("A", "GeometryNodeTransform"), ("B", "GeometryNodeTransform"),
             ("Join", "GeometryNodeJoinGeometry"), ("Out", "NodeGroupOutput")],
            [("A", "Geometry", "Join", "Geometry", 0, True), ("B", "Geometry", "Join", "Geometry", 1, True),
             ("A", "Geometry", "Out", "Geometry", 0, False)],
        )
        specs = [
            {"from_node": "A", "from_socket": "Geometry", "to_node": "Join", "to_socket": "Geometry"},
            {"from_node": "A", "from_socket": "Geometry", "to_node": "Out", "to_socket": "Geometry"},
        ]
        plan, problems = module.plan_reroute_insert(graph, specs, None, True)
        self.assertEqual(problems, [])
        self.assertEqual(len(plan["reroutes"]), 1)
        self.assertEqual(len(plan["reroutes"][0]["targets"]), 2)
        # the multi-input socket needs its substituted order, the other does not
        self.assertEqual([entry["socket"] for entry in plan["order"]], ["Join.Geometry"])
        self.assertEqual([entry["kind"] for entry in plan["order"][0]["entries"]], ["new", "keep"])

        plan, problems = module.plan_reroute_insert(graph, specs, None, False)
        self.assertEqual(len(plan["reroutes"]), 2)

        fan_in = [
            {"from_node": "A", "from_socket": "Geometry", "to_node": "Join", "to_socket": "Geometry"},
            {"from_node": "B", "from_socket": "Geometry", "to_node": "Join", "to_socket": "Geometry"},
        ]
        plan, problems = module.plan_reroute_insert(graph, fan_in, [[1.0, 1.0], [2.0, 2.0]], True)
        self.assertEqual(problems, [])
        self.assertEqual(len(plan["reroutes"]), 2)
        self.assertEqual([entry["socket"] for entry in plan["order"]], ["Join.Geometry"])
        self.assertEqual(len(plan["order"][0]["entries"]), 2)

    def test_plan_insert_reports_every_preflight_problem(self):
        module = self.module
        graph = self.graph(
            [("A", "GeometryNodeTransform"), ("Join", "GeometryNodeJoinGeometry")],
            [("A", "Geometry", "Join", "Geometry", 5, True)],
        )
        base = {"from_node": "A", "from_socket": "Geometry", "to_node": "Join", "to_socket": "Geometry"}
        _plan, problems = module.plan_reroute_insert(graph, [base], None, False)
        self.assertEqual(problems, [])
        _plan, problems = module.plan_reroute_insert(
            graph, [dict(base, multi_input_sort_id=9)], None, False)
        self.assertEqual(problems[0]["code"], "sort_id_mismatch")
        _plan, problems = module.plan_reroute_insert(
            graph, [{"from_node": "A", "from_socket": "Geometry", "to_node": "Nope", "to_socket": "Geometry"}],
            None, False)
        self.assertEqual(problems[0]["code"], "link_not_found")
        _plan, problems = module.plan_reroute_insert(graph, [base, base], None, False)
        self.assertEqual(problems[0]["code"], "duplicate_link_spec")
        _plan, problems = module.plan_reroute_insert(graph, [base], [[0.0, 0.0], [1.0, 1.0]], False)
        self.assertEqual(problems[0]["code"], "points_count_mismatch")
        _plan, problems = module.plan_reroute_insert(graph, [base] * 200, None, False)
        self.assertEqual(problems[0]["code"], "too_many_links")

    def test_plan_delete_refuses_cycles_and_merges(self):
        module = self.module
        chain = self.graph(
            [("S", "GeometryNodeTransform"), ("R1", "NodeReroute"), ("R2", "NodeReroute"),
             ("Join", "GeometryNodeJoinGeometry")],
            [("S", "Geometry", "R1", "Input", 0, False), ("R1", "Output", "R2", "Input", 0, False),
             ("R2", "Output", "Join", "Geometry", 0, True)],
        )
        plan, problems = module.plan_reroute_delete(chain, ["R1", "R2"])
        self.assertEqual(problems, [])
        self.assertEqual(plan["reconnects"][0]["source"], {"node": "S", "socket": "Geometry"})
        self.assertEqual(len(plan["order"][0]["entries"]), 1)

        merge = self.graph(
            [("S", "GeometryNodeTransform"), ("R1", "NodeReroute"), ("R2", "NodeReroute"),
             ("Join", "GeometryNodeJoinGeometry")],
            [("S", "Geometry", "R1", "Input", 0, False), ("S", "Geometry", "R2", "Input", 0, False),
             ("R1", "Output", "Join", "Geometry", 0, True), ("R2", "Output", "Join", "Geometry", 1, True)],
        )
        _plan, problems = module.plan_reroute_delete(merge, ["R1", "R2"])
        self.assertEqual(problems[0]["code"], "delete_would_merge_sources")

        cycle = self.graph(
            [("R1", "NodeReroute"), ("R2", "NodeReroute")],
            [("R2", "Output", "R1", "Input", 0, False), ("R1", "Output", "R2", "Input", 0, False)],
        )
        _plan, problems = module.plan_reroute_delete(cycle, ["R1"])
        self.assertEqual(problems[0]["code"], "reroute_cycle")

    def test_midpoint_position_is_deterministic(self):
        module = self.module
        graph = self.graph([("A", "GeometryNodeTransform"), ("B", "GeometryNodeTransform")], [])
        graph["nodes"][0]["location"] = [0.0, 0.0]
        graph["nodes"][1]["location"] = [100.0, 50.0]
        self.assertEqual(module.midpoint_position(graph["nodes"], "A", ["B"]), [50.0, 25.0])
        self.assertEqual(module.midpoint_position(graph["nodes"], "Missing", ["B"]), [50.0, 25.0])


class ServerToolTests(unittest.TestCase):
    """The MCP surface: names, schema defaults and forwarded bridge commands."""

    @classmethod
    def setUpClass(cls):
        from mcp.server.fastmcp import FastMCP
        from blender_mcp.tools import geometry_nodes_edit

        cls.module = geometry_nodes_edit
        cls.calls = []

        class StubBridge:
            def call(self, command, params=None, timeout=None):
                cls.calls.append((command, params))
                return {"command": command, "params": params}

        cls.app = FastMCP("gn-edit-test")
        geometry_nodes_edit.register(cls.app, StubBridge())
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.app.list_tools())}

    def test_exactly_five_named_tools(self):
        self.assertEqual(sorted(self.tools), [
            "blender_geometry_nodes_layout",
            "blender_geometry_nodes_move_nodes",
            "blender_geometry_nodes_reroute_delete",
            "blender_geometry_nodes_reroute_insert",
            "blender_geometry_nodes_reroute_move",
        ])

    def test_schema_contract(self):
        insert = self.tools["blender_geometry_nodes_reroute_insert"].inputSchema
        self.assertEqual(sorted(insert["properties"]), ["group", "links", "points", "shared_branch"])
        self.assertEqual(insert["required"], ["group", "links"])
        self.assertFalse(insert["properties"]["shared_branch"]["default"])
        points = insert["properties"]["points"]
        self.assertIn("anyOf", points)
        self.assertTrue(any(option.get("type") == "array" for option in points["anyOf"]))
        move = self.tools["blender_geometry_nodes_move_nodes"].inputSchema
        self.assertEqual(sorted(move["properties"]), ["absolute", "group", "positions"])
        self.assertFalse(move["properties"]["absolute"]["default"])
        layout = self.tools["blender_geometry_nodes_layout"].inputSchema
        self.assertEqual(sorted(layout["properties"]), ["group", "modifier", "object"])
        self.assertEqual(layout["required"], ["group"])
        delete = self.tools["blender_geometry_nodes_reroute_delete"].inputSchema
        self.assertEqual(delete["required"], ["group", "nodes"])

    def test_tools_forward_the_exact_bridge_command(self):
        asyncio.run(self.app.call_tool("blender_geometry_nodes_layout", {"group": "G"}))
        self.assertEqual(self.calls[-1], ("geometry_nodes_layout", {"group": "G", "object": None, "modifier": None}))
        asyncio.run(self.app.call_tool("blender_geometry_nodes_move_nodes",
                                      {"group": "G", "positions": [{"node": "A", "x": 1, "y": 2}]}))
        self.assertEqual(self.calls[-1][0], "geometry_nodes_move_nodes")
        self.assertEqual(self.calls[-1][1]["absolute"], False)
        asyncio.run(self.app.call_tool("blender_geometry_nodes_reroute_insert",
                                      {"group": "G", "links": [{"from_node": "A", "from_socket": "Geometry",
                                                                "to_node": "B", "to_socket": "Geometry"}]}))
        self.assertEqual(self.calls[-1][0], "geometry_nodes_reroute_insert")
        self.assertEqual(self.calls[-1][1]["shared_branch"], False)
        self.assertIsNone(self.calls[-1][1]["points"])
        asyncio.run(self.app.call_tool("blender_geometry_nodes_reroute_move",
                                      {"group": "G", "positions": [{"node": "Reroute", "x": 0, "y": 0}]}))
        self.assertEqual(self.calls[-1][0], "geometry_nodes_reroute_move")
        asyncio.run(self.app.call_tool("blender_geometry_nodes_reroute_delete", {"group": "G", "nodes": ["Reroute"]}))
        self.assertEqual(self.calls[-1], ("geometry_nodes_reroute_delete", {"group": "G", "nodes": ["Reroute"]}))

    def test_tool_module_never_imports_bpy(self):
        source = TOOLS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)
        self.assertNotIn("from bpy", source)

    def test_bridge_module_imports_only_bpy_and_math(self):
        source = textwrap.dedent(BRIDGE_PATH.read_text(encoding="utf-8"))
        imports = sorted(
            line.strip() for line in source.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("from ")
        )
        self.assertEqual(imports, ["from __future__ import annotations", "import bpy", "import math"])


if __name__ == "__main__":
    unittest.main()
