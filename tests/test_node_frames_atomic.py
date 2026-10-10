"""Failure injection and ownership regressions for native Frame commands."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class Failure(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class Collection(list):
    def get(self, name):
        return next((n for n in self if n.name == name), None)
    def new(self, name):
        n = Node("Frame") if name == "NodeFrame" else Text(name)
        self.append(n)
        return n


class Node(dict):
    def __init__(self, name, frame=True):
        super().__init__()
        self.name, self.bl_idname = name, "NodeFrame" if frame else "TestNode"
        self.location, self._parent, self._width, self.height = [0., 0.], None, 200., 200.
        self.dimensions, self.label, self.label_size, self.shrink = [0, 0], "old", 24, False
        self.color, self.use_custom_color, self.text = [.1, .1, .1], False, None
        self.fail_parent = self.fail_width = False
    def as_pointer(self):
        return id(self)
    @property
    def parent(self):
        return self._parent
    @parent.setter
    def parent(self, value):
        if self.fail_parent:
            self.fail_parent = False
            raise RuntimeError("injected parent failure")
        self._parent = value
    @property
    def width(self):
        return self._width
    @width.setter
    def width(self, value):
        if self.fail_width:
            self.fail_width = False
            raise RuntimeError("injected width failure")
        self._width = value
    def __bool__(self):
        return True


class Text(dict):
    def __init__(self, name):
        super().__init__()
        self.name, self.body, self.users, self.use_fake_user = name, "original", 1, False
        self.library = self.override_library = None
        self.filepath, self.fail = "", False
    def as_pointer(self):
        return id(self)
    def as_string(self):
        return self.body
    def from_string(self, value):
        if self.fail:
            self.fail = False
            self.body = "partially written"
            raise RuntimeError("injected write failure")
        self.body = value
    def __bool__(self):
        return True


class Tests(unittest.TestCase):
    def setUp(self):
        self.tree = types.SimpleNamespace(name="Group", bl_idname="GeometryNodeTree", library=None, override_library=None,
                                          nodes=Collection(), links=[], update_tag=lambda: None, as_pointer=lambda: 100)
        self.frame, self.a, self.b = Node("Frame"), Node("A", False), Node("B", False)
        self.tree.nodes.extend([self.frame, self.a, self.b])
        self.a.location, self.b.location = [10, 20], [50, 60]
        data = types.SimpleNamespace(node_groups=[self.tree], texts=Collection(), materials=[], worlds=[], lights=[], scenes=[])
        self.data = data
        sys.modules["bpy"] = types.SimpleNamespace(data=data)
        path = Path(__file__).resolve().parents[1] / "blender" / "node_frames.py"
        spec = importlib.util.spec_from_file_location("native_frame_test", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.commands = {}
        def command(name, **kwargs):
            def wrapped(func):
                self.commands[name] = func
                return func
            return wrapped
        self.module.register_commands(types.SimpleNamespace(CommandError=Failure, command=command))

    def call(self, cmd, **params):
        return self.commands["node_frame_" + cmd](group="Group", **params)

    def error(self, code, cmd, **params):
        with self.assertRaises(Failure) as caught:
            self.call(cmd, **params)
        self.assertEqual(caught.exception.code, code)

    def test_invalid_batch_does_not_create_or_reparent(self):
        self.error("node_not_found", "parent", frame="New", nodes=["A", "Missing"], create_frame=True)
        self.assertEqual(len(self.tree.nodes), 3)
        self.assertIsNone(self.a.parent)

    def test_partial_parent_failure_restores_all(self):
        self.b.fail_parent = True
        self.error("frame_write_failed", "parent", frame="Frame", nodes=["A", "B"])
        self.assertIsNone(self.a.parent)
        self.assertIsNone(self.b.parent)
        self.assertEqual(list(self.a.location), [10, 20])
        self.assertEqual(list(self.b.location), [50, 60])

    def test_property_failure_restores_label_and_width(self):
        self.frame.fail_width = True
        self.error("frame_write_failed", "update", frame="Frame", label="new", width=500)
        self.assertEqual(self.frame.label, "old")
        self.assertEqual(self.frame.width, 200)

    def test_shared_text_refused(self):
        text = Text("Shared")
        self.frame.text = text
        other = Node("Other")
        other.text = text
        self.tree.nodes.append(other)
        self.error("shared_text", "set_text", frame="Frame", body="overwrite", overwrite=True)
        self.assertEqual(text.body, "original")

    def test_user_text_default_protected(self):
        self.frame.text = Text("User")
        self.error("text_exists", "set_text", frame="Frame", body="overwrite")
        self.assertEqual(self.frame.text.body, "original")

    def test_write_failure_restores_body(self):
        text = Text("User")
        self.frame.text = text
        text.fail = True
        self.error("frame_write_failed", "set_text", frame="Frame", body="overwrite", overwrite=True)
        self.assertEqual(text.body, "original")
        self.assertIs(self.frame.text, text)

    def test_ambiguous_tree_rejected(self):
        self.data.node_groups.append(types.SimpleNamespace(name="Group"))
        self.error("ambiguous_name", "get", frame="Frame")

    def test_linked_tree_refused_before_write(self):
        self.tree.library = object()
        self.error("read_only_tree", "update", frame="Frame", label="new")
        self.assertEqual(self.frame.label, "old")

    def test_cycle_rejected(self):
        inner = Node("Inner")
        inner.parent = self.frame
        self.tree.nodes.append(inner)
        self.error("parent_cycle", "parent", frame="Inner", nodes=["Frame"])
        self.assertIsNone(self.frame.parent)


if __name__ == "__main__":
    unittest.main()
