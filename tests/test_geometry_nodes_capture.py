"""Offline regressions for the Geometry Nodes editor capture.

No Blender, no socket and no real ``bpy``: the bridge modules
(``blender/review_capture_common.py`` and ``blender/geometry_nodes_capture.py``)
are loaded with a small in-process stub of the Blender API they use, which makes
the *real* command code testable - including the deferred draw gate, the UI
restore and the atomic publication - without pretending that Blender ran.

The MCP side is exercised through the real FastMCP tool objects with a scripted
bridge, so the image really crosses ``image_result`` as ``ImageContent``.

Live Blender 5.2.1 acceptance is the project owner's step; ``--live`` runs the
same checks through the official MCP stdio client and writes a report.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.util
import json
import os
import shutil
import struct
import sys
from unittest.mock import patch
from contextlib import contextmanager
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
BLENDER_DIR = ROOT / "blender"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BLENDER_DIR))
REPORT = ROOT / ".runtime" / "gn_capture_acceptance.json"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# a real PNG, built with the standard library only
# --------------------------------------------------------------------------- #
def png_bytes(width=64, height=48, fill=b"\x30\x60\x90"):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + fill * width for _ in range(height))
    return PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


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


class FakeCollection(list):
    def get(self, name, default=None):
        for item in self:
            if getattr(item, "name", None) == name:
                return item
        return default

    def __contains__(self, item):
        if isinstance(item, str):
            return any(getattr(candidate, "name", None) == item for candidate in self)
        return list.__contains__(self, item)


class FakeProp:
    def __init__(self, identifier, type="BOOLEAN", readonly=False):
        self.identifier, self.type, self.is_readonly = identifier, type, readonly


class FakeRna:
    def __init__(self, names):
        self.properties = [FakeProp(name) for name in names]


class FakeSocket:
    def __init__(self, identifier, name="Geometry", socket_type="GEOMETRY", index=0, multi=False):
        self.identifier, self.name, self.type, self.index = identifier, name, socket_type, index
        self.is_linked = False
        self.is_multi_input = multi
        self.enabled, self.hide = True, False


class FakeNode:
    bl_idname = "GeometryNodeTransform"

    def __init__(self, name, location=(0.0, 0.0), parent=None, label="", width=160.0, height=120.0):
        self.name, self.label, self.parent = name, label, parent
        self.location = list(location)
        self.width, self.height, self.dimensions = width, height, [width, height]
        self.select, self.hide, self.mute = False, False, False
        self.internal_links = []
        self.inputs = [FakeSocket("Geometry", index=0)]
        self.outputs = [FakeSocket("Geometry", index=0)]
        self.id_data = None
        self.type = self.bl_idname

    def as_pointer(self):
        return id(self)


class FakeFrame(FakeNode):
    bl_idname = "NodeFrame"

    def __init__(self, name, location=(0.0, 0.0), parent=None, label="", width=180.0, height=100.0):
        super().__init__(name, location, parent, label, width, height)
        self.inputs, self.outputs = [], []
        self.shrink, self.use_custom_color = False, False
        self.color = [0.2, 0.3, 0.4]
        self.label_size = 24
        self.text = None


class FakeReroute(FakeNode):
    bl_idname = "NodeReroute"

    def __init__(self, name, location=(0.0, 0.0), parent=None):
        super().__init__(name, location, parent, "", 20.0, 20.0)
        self.inputs = [FakeSocket("Input", name="Input", socket_type="GEOMETRY", index=0)]
        self.outputs = [FakeSocket("Output", name="Output", socket_type="GEOMETRY", index=0)]


class FakeLink:
    def __init__(self, from_node, from_socket, to_node, to_socket, sort_id=0):
        self.from_node, self.from_socket = from_node, from_socket
        self.to_node, self.to_socket = to_node, to_socket
        self.multi_input_sort_id = sort_id
        self.is_valid = True


class FakePath:
    def __init__(self, trees=None):
        self.trees = list(trees or [])

    def __len__(self):
        return len(self.trees)

    def __iter__(self):
        return iter([NS(node_tree=tree) for tree in self.trees])

    def clear(self):
        self.trees = []

    def start(self, tree):
        self.trees = [tree]

    def append(self, tree):
        self.trees.append(tree)


class FakeNodeSpace:
    def as_pointer(self):
        return id(self)

    type = "NODE_EDITOR"
    FIELDS = ("tree_type", "shader_type", "geometry_nodes_type", "pin", "show_region_ui")

    def __init__(self, tree=None, tree_type="GeometryNodeTree"):
        self.node_tree = tree
        self.tree_type = tree_type
        self.shader_type = "Shader"
        self.geometry_nodes_type = "MODIFIER"
        self.pin = False
        self.show_region_ui = False
        self.overlay = None
        self.id = None
        self.path = FakePath()
        self.bl_rna = FakeRna(self.FIELDS)
        self.handlers = []
        self.fail_tree_assignment = False
        self.fail_ui_toggle = False

    # a real Space.draw_handler_add/remove pair
    def draw_handler_add(self, callback, args, region_type, draw_type):
        handler = (callback, args, region_type, draw_type)
        self.handlers.append(handler)
        return handler

    def draw_handler_remove(self, handler, region_type):
        self.handlers.remove(handler)

    def paint(self):
        """Simulate one real draw pass of this editor."""
        for callback, args, _region, _type in list(self.handlers):
            session = args[0]
            with sys.modules['bpy'].context.temp_override(
                    window=session['window'], area=session['area'], region=session['region']):
                callback(*args)

    def __setattr__(self, name, value):
        if name == "node_tree" and getattr(self, "fail_tree_assignment", False) and value is not None:
            # mimic a pinned editor that refuses to switch trees
            return
        if name == "show_region_ui" and getattr(self, "fail_ui_toggle", False):
            raise RuntimeError("UI region cannot be toggled")
        object.__setattr__(self, name, value)


class FakeRegion3D:
    def __init__(self):
        self.view_location = [0.0, 0.0, 0.0]
        self.view_rotation = [1.0, 0.0, 0.0, 0.0]
        self.view_distance = 10.0
        self.view_perspective = "PERSP"


class FakeView3DSpace:
    def as_pointer(self):
        return id(self)

    type = "VIEW_3D"
    FIELDS = ("show_region_ui", "show_gizmo")

    def __init__(self):
        self.region_3d = FakeRegion3D()
        self.show_region_ui = False
        self.show_gizmo = True
        self.overlay = NS(bl_rna=FakeRna(["show_overlays"]), show_overlays=True)
        self.bl_rna = FakeRna(self.FIELDS)


class FakeArea:
    def as_pointer(self):
        return id(self)

    def __init__(self, area_type, space=None, width=900, height=640):
        self._type = area_type
        self.width, self.height = width, height
        self._spaces = {area_type: space or (FakeNodeSpace() if area_type == "NODE_EDITOR"
                                             else FakeView3DSpace())}
        self.spaces = FakeCollection([self._spaces[area_type]])
        self.regions = [NS(type="WINDOW", width=width, height=height, x=0, y=0)]
        self.redraws = 0

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        # Blender keeps one SpaceLink per editor type per area: switching back
        # returns the previous space object (and its state).
        self._type = value
        if value not in self._spaces:
            self._spaces[value] = FakeNodeSpace() if value == "NODE_EDITOR" else FakeView3DSpace()
        self.spaces = FakeCollection([self._spaces[value],
                                     *[s for k, s in self._spaces.items() if k != value]])

    def tag_redraw(self):
        self.redraws += 1


class FakeLayer:
    def __init__(self, name="ViewLayer"):
        self.name = name
        self.objects = FakeCollection()
        self.objects.active = None


class FakeScreen:
    def __init__(self, areas):
        self.areas = FakeCollection(areas)


class FakeWindow:
    def __init__(self, scene, areas, layer=None):
        self.scene = scene
        self.view_layer = layer or FakeLayer()
        self.screen = FakeScreen(areas)


class FakeObjRef:
    def __init__(self, filepath):
        self.filepath = filepath


class FakeTree:
    def as_pointer(self):
        return id(self)

    def __init__(self, name="GN_Test", bl_idname="GeometryNodeTree"):
        self.name, self.bl_idname = name, bl_idname
        self.nodes = FakeCollection()
        self.links = []
        self.interface = NS(items_tree=[])
        self.users = 2
        self.library = None
        self.override_library = None
        self.is_embedded_data = False
        self.use_fake_user = False

    def add(self, node):
        node.id_data = self
        self.nodes.append(node)
        return node

    def connect(self, from_node, from_socket, to_node, to_socket, sort_id=0):
        link = FakeLink(from_node, from_node.outputs[from_socket], to_node, to_node.inputs[to_socket], sort_id)
        self.links.append(link)
        return link


class FakeModifier:
    def __init__(self, name="Nodes", node_group=None, mod_type="NODES"):
        self.name, self.type, self.node_group = name, mod_type, node_group
        self.show_viewport = self.show_render = True


class FakeObject:
    def __init__(self, name, tree=None, mod_name="Nodes"):
        self.name, self.type, self.data = name, "MESH", NS(name=name + "_mesh")
        self.modifiers = [FakeModifier(mod_name, tree)] if tree is not None else []
        self.matrix_world = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
        self.hide_viewport = False
        self._selected = False


class FakeScene:
    def __init__(self, name="Scene"):
        self.name = name
        self.objects = FakeCollection()
        self.frame_current = 1
        self.unit_settings = NS(system="METRIC")


class FakeOverride:
    def __init__(self, context, values):
        self.context, self.values, self.saved = context, values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = getattr(self.context, key, None)
            setattr(self.context, key, value)
        return self.context

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            setattr(self.context, key, value)
        return False


class FakeContext:
    def __init__(self, window, scene):
        self.window, self.scene, self.mode = window, scene, "OBJECT"
        self.view_layer = window.view_layer
        self.window_manager = NS(windows=[window])

    @property
    def selected_objects(self):
        return [obj for obj in self.view_layer.objects if obj._selected]

    def temp_override(self, **values):
        return FakeOverride(self, values)


class FakeOperator:
    def __init__(self, on_call=None, result=("FINISHED",)):
        self.calls = []
        self.on_call, self.result = on_call, result

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if callable(self.on_call):
            self.on_call(**kwargs)
        return set(self.result)


class FakeSpaceScreen:
    def __init__(self, screenshot):
        self.screenshot_area = screenshot


class FakeNodeOps:
    def __init__(self):
        self.view_all = FakeOperator()
        self.view_selected = FakeOperator()
        self.tree_path_parent = FakeOperator()


class FakeWmOps:
    def __init__(self, bpy_stub, redraw_areas=False):
        self.bpy, self.redraw_areas = bpy_stub, redraw_areas
        self.redraw_timer = FakeOperator(on_call=self._redraw)

    def _redraw(self, **kwargs):
        if not self.redraw_areas:
            return
        for window in self.bpy.data.window_managers[0].windows:
            for area in window.screen.areas:
                for space in area.spaces:
                    paint = getattr(space, "paint", None)
                    if callable(paint):
                        paint()


class FakeBpy:
    def __init__(self, area, scene, screenshot=None, redraw_areas=False):
        self.window = FakeWindow(scene, [area])
        layer = self.window.view_layer
        self.data = NS(window_managers=[NS(windows=[self.window])], node_groups=FakeCollection(), objects=FakeCollection(),
                       scenes=[scene], is_dirty=False)
        self.context = FakeContext(self.window, scene)
        self.app = NS(background=False, timers=NS(registered=[], register=lambda fn, first_interval=0: (
            self.app.timers.registered.append(fn), None)[1]))
        self.ops = NS(screen=FakeSpaceScreen(screenshot or FakeOperator(
            on_call=lambda filepath, **kw: Path(filepath).write_bytes(png_bytes()))),
            node=FakeNodeOps(), wm=FakeWmOps(self, redraw_areas))
        self.saved_bytes = None


class FakeApi:
    """The subset of the add-on module the bridge modules use."""

    CommandError = Failure

    def __init__(self, root, tree=None):
        self.root = Path(root)
        self.commands = {}
        self.modifier_info_calls = []

    def command(self, name, mutating=True, summary=""):
        def decorate(func):
            self.commands[name] = func
            return func
        return decorate

    def project_dir(self):
        return self.root / "Project"

    def runtime_dir(self):
        return self.root / ".runtime"

    def resolve_project_path(self, relative):
        raw = str(relative).replace("\\", "/")
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise Failure("invalid_path", "absolute paths are rejected")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise Failure("invalid_path", "path traversal ('..') is rejected")
        root = (self.root / "Project").resolve()
        return root.joinpath(*parts)

    def _modifier_info(self, obj, mod, deps=None):
        self.modifier_info_calls.append((obj.name, mod.name, deps))
        return {"name": mod.name, "type": mod.type, "node_group": getattr(mod.node_group, "name", None),
                "is_geometry_nodes": True, "show_viewport": bool(mod.show_viewport)}


def load_bridge_modules(bpy_stub):
    """Import both bridge modules against a stub ``bpy`` (fresh each call)."""
    previous = sys.modules.get("bpy")
    sys.modules["bpy"] = bpy_stub
    try:
        for name in ("review_capture_common", "geometry_nodes_capture"):
            sys.modules.pop(name, None)
        common = importlib.import_module("review_capture_common")
        module = importlib.import_module("geometry_nodes_capture")
        return common, module
    finally:
        if previous is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = previous


def build_tree():
    tree = FakeTree("GN_Test")
    frame = tree.add(FakeFrame("FrameA", location=(-200.0, 300.0), label="Stage"))
    source = tree.add(FakeNode("Input", location=(-400.0, 0.0)))
    process = tree.add(FakeNode("Process", location=(0.0, 0.0), parent=frame))
    reroute = tree.add(FakeReroute("Reroute", location=(200.0, 0.0)))
    output = tree.add(FakeNode("Output", location=(400.0, 0.0)))
    tree.connect(source, 0, process, 0)
    tree.connect(process, 0, reroute, 0)
    tree.connect(reroute, 0, output, 0)
    return tree


class AddonCase(unittest.TestCase):
    """Common fixture: one tree, one window, an isolated project directory."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".gn_capture_", dir=str(ROOT.parent / "Project")))
        (self.root / "Project" / PREVIEWS).mkdir(parents=True, exist_ok=True)
        (self.root / ".runtime").mkdir(parents=True, exist_ok=True)
        self.tree = build_tree()
        self.scene = FakeScene("Main")
        self.area = FakeArea("VIEW_3D")
        self.bpy = FakeBpy(self.area, self.scene)
        self.bpy.data.node_groups.append(self.tree)
        self.api = FakeApi(self.root)
        self._previous_bpy = sys.modules.get("bpy")
        self.common, self.module = load_bridge_modules(self.bpy)
        sys.modules["bpy"] = self.bpy
        self.module.register_commands(self.api)
        self.addCleanup(self._teardown)

    def _teardown(self):
        if self._previous_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = self._previous_bpy
        shutil.rmtree(self.root, ignore_errors=True)

    def command(self, name):
        return self.api.commands[name]

    def prepare(self, **kwargs):
        params = {"group": "GN_Test"}
        params.update(kwargs)
        return self.command("gn_editor_capture_prepare")(**params)

    def editor_space(self):
        for area in self.bpy.window.screen.areas:
            if area.type == "NODE_EDITOR":
                return area, area.spaces[0]
        raise AssertionError("no node editor area")


PREVIEWS = "previews"


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #
class SchemaTests(unittest.TestCase):
    def _tools(self):
        from mcp.server.fastmcp import FastMCP
        from blender_mcp.tools import geometry_nodes_capture

        class Stub:
            def call(self, *args, **kwargs):
                return {}

        mcp = FastMCP("gn-capture-schema")
        geometry_nodes_capture.register(mcp, Stub())
        return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    def test_single_tool_schema_and_defaults(self):
        tools = self._tools()
        self.assertEqual(list(tools), ["blender_geometry_nodes_capture_editor"])
        schema = tools["blender_geometry_nodes_capture_editor"].inputSchema
        self.assertEqual(schema["required"], ["group"])
        self.assertEqual(schema["properties"]["fit"]["default"], "KEEP")
        self.assertEqual(sorted(schema["properties"]["fit"]["enum"]), ["ALL", "KEEP", "SELECTED"])
        self.assertIs(schema["properties"]["include_sidebar"]["default"], True)
        self.assertIs(schema["properties"]["overwrite"]["default"], False)
        self.assertEqual(next(s for s in schema["properties"]["nodes"]["anyOf"] if s.get("type") == "array")["maxItems"], 512)
        for name in ("object", "modifier", "window_index", "area_index", "relative_path"):
            self.assertIn(name, schema["properties"])

    def test_module_does_not_import_bpy(self):
        source = (ROOT / "blender_mcp" / "tools" / "geometry_nodes_capture.py").read_text(encoding="utf-8")
        helper = (ROOT / "blender_mcp" / "tools" / "review_image.py").read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)
        self.assertNotIn("import bpy", helper)

    def test_neighbouring_schemas_unchanged(self):
        from mcp.server.fastmcp import FastMCP
        from blender_mcp.tools import geometry_nodes_capture, preview

        class Stub:
            def call(self, *args, **kwargs):
                return {}

        alone = FastMCP("alone")
        preview.register(alone, Stub())
        baseline = {t.name: t.model_dump(exclude={"description"}) for t in asyncio.run(alone.list_tools())}
        both = FastMCP("both")
        preview.register(both, Stub())
        geometry_nodes_capture.register(both, Stub())
        current = {t.name: t.model_dump(exclude={"description"}) for t in asyncio.run(both.list_tools())}
        self.assertTrue(all(current[name] == value for name, value in baseline.items()))
        self.assertIn("blender_geometry_nodes_capture_editor", current)


# --------------------------------------------------------------------------- #
# review_image: paths, evidence, native image content
# --------------------------------------------------------------------------- #
class ReviewImageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".gn_review_", dir=str(ROOT.parent / "Project")))
        self.previews = self.root / "previews"
        self.previews.mkdir(parents=True, exist_ok=True)
        self._previous = os.environ.get("BLENDER_MCP_PROJECT_DIR")
        os.environ["BLENDER_MCP_PROJECT_DIR"] = str(self.root)
        self.addCleanup(self._teardown)
        from blender_mcp.tools import review_image
        self.review = review_image

    def _teardown(self):
        if self._previous is None:
            os.environ.pop("BLENDER_MCP_PROJECT_DIR", None)
        else:
            os.environ["BLENDER_MCP_PROJECT_DIR"] = self._previous
        shutil.rmtree(self.root, ignore_errors=True)

    def _publish(self, name="editor.png", width=64, height=48, mtime=None):
        target = self.previews / name
        raw = png_bytes(width, height)
        target.write_bytes(raw)
        if mtime is not None:
            os.utime(target, (mtime, mtime))
        return target, raw

    def test_image_result_returns_text_then_image(self):
        target, raw = self._publish()
        started = time.time() - 1.0
        content = self.review.image_result({
            "path": str(target), "relative_path": "previews/editor.png", "bytes": len(raw),
            "width": 64, "height": 48, "sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "mtime": target.stat().st_mtime, "image_base64": base64.b64encode(raw).decode("ascii"),
            "image_source": "bpy.types.Window.screenshot",
        }, started=started)
        self.assertEqual([block.type for block in content], ["text", "image"])
        self.assertEqual(content[1].mimeType, "image/png")
        self.assertEqual(base64.b64decode(content[1].data), raw)
        metadata = json.loads(content[0].text)
        self.assertTrue(metadata["mcp_image_content"])
        self.assertNotIn("image_base64", metadata)
        self.assertEqual(metadata["relative_path"], "previews/editor.png")
        self.assertIn("bpy.types.Window.screenshot", metadata["note"])
        self.assertNotIn("bpy.ops.screen.screenshot_area", metadata["note"])

    def test_stale_capture_is_rejected(self):
        target, raw = self._publish(mtime=time.time() - 600)
        with self.assertRaises(self.review.ReviewImageError) as caught:
            self.review.image_result({
                "path": str(target), "bytes": len(raw), "width": 64, "height": 48,
                "mtime": target.stat().st_mtime, "image_base64": base64.b64encode(raw).decode("ascii"),
            }, started=time.time())
        self.assertIn("stale", str(caught.exception))

    def test_hash_mismatch_and_byte_mismatch_are_rejected(self):
        target, raw = self._publish()
        for metadata in (
            {"path": str(target), "bytes": len(raw), "width": 64, "height": 48,
             "sha256": "0" * 64, "image_base64": base64.b64encode(raw).decode("ascii")},
            {"path": str(target), "bytes": len(raw) + 5, "width": 64, "height": 48,
             "image_base64": base64.b64encode(raw).decode("ascii")},
            {"path": str(target), "bytes": len(raw), "width": 32, "height": 48,
             "image_base64": base64.b64encode(raw).decode("ascii")},
        ):
            with self.assertRaises(self.review.ReviewImageError):
                self.review.image_result(dict(metadata))

    def test_missing_image_data_is_rejected(self):
        target, _raw = self._publish()
        with self.assertRaises(self.review.ReviewImageError):
            self.review.image_result({"path": str(target), "bytes": 10})
        with self.assertRaises(self.review.ReviewImageError):
            self.review.image_result({"path": str(target), "image_base64": base64.b64encode(b"not a png").decode()})

    def test_path_rules(self):
        for bad in ("editor.png", "/abs/editor.png", "C:/abs/editor.png",
                    "previews/../editor.png", "other/editor.png", "previews/editor.jpg", ""):
            with self.assertRaises(self.review.ReviewImageError, msg=bad):
                self.review.validate_relative_path(bad)
        relative, target = self.review.validate_relative_path("previews/sub/editor.png")
        self.assertEqual(relative, "previews/sub/editor.png")
        self.assertTrue(str(target).startswith(str(self.previews)))

    def test_default_path_is_collision_free_and_sanitised(self):
        first = self.review.default_relative_path("gn-editor", "GN_Test/../x")
        second = self.review.default_relative_path("gn-editor", "GN_Test/../x")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("previews/"))
        self.assertTrue(first.endswith(".png"))
        self.assertNotIn("..", first)
        self.assertNotIn("/../", first)


# --------------------------------------------------------------------------- #
# MCP orchestration: prepare -> poll -> commit -> abort
# --------------------------------------------------------------------------- #
class DeferredCaptureTests(unittest.TestCase):
    class Bridge:
        def __init__(self, statuses, commit=None, fail_prepare=None):
            self.statuses = list(statuses)
            self.commit = commit or {}
            self.fail_prepare = fail_prepare
            self.calls = []

        def call(self, command, params=None, timeout=None):
            self.calls.append(command)
            if command.startswith("gn_editor_capture_prepare"):
                if self.fail_prepare:
                    raise self.fail_prepare
                return {"session": "sess-1", "armed": True}
            if command.startswith("gn_editor_capture_status"):
                return self.statuses.pop(0) if self.statuses else {"draws_after_arm": 0, "match": {"matches": False}}
            if command.startswith("gn_editor_capture_commit"):
                return dict(self.commit, image_base64=None)
            return {"aborted": True}

    def test_waits_for_a_confirmed_draw_then_commits_and_aborts(self):
        from blender_mcp.tools import review_image
        bridge = self.Bridge([
            {"draws_after_arm": 0, "match": {"matches": False}},
            {"draws_after_arm": 2, "match": {"matches": True}},
        ], commit={"path": "x", "bytes": 1})
        result, orchestration = review_image.run_deferred_capture(
            bridge, prepare_command="gn_editor_capture_prepare", status_command="gn_editor_capture_status",
            commit_command="gn_editor_capture_commit", abort_command="gn_editor_capture_abort",
            prepare_params={"group": "G"}, relative_path="previews/x.png", overwrite=False,
            wait_seconds=2.0, poll_interval=0.001)
        self.assertEqual(result["path"], "x")
        self.assertEqual(orchestration["session"], "sess-1")
        self.assertEqual(bridge.calls[-1], "gn_editor_capture_abort")
        self.assertEqual(bridge.calls.count("gn_editor_capture_commit"), 1)

    def test_no_draw_means_no_commit(self):
        from blender_mcp.tools import review_image
        bridge = self.Bridge([{"draws_after_arm": 0, "match": {"matches": False}}] * 50)
        with self.assertRaises(review_image.ReviewImageError) as caught:
            review_image.run_deferred_capture(
                bridge, prepare_command="gn_editor_capture_prepare", status_command="gn_editor_capture_status",
                commit_command="gn_editor_capture_commit", abort_command="gn_editor_capture_abort",
                prepare_params={"group": "G"}, relative_path="previews/x.png", overwrite=False,
                wait_seconds=0.05, poll_interval=0.005)
        self.assertIn("no redraw", str(caught.exception))
        self.assertNotIn("gn_editor_capture_commit", bridge.calls)
        self.assertEqual(bridge.calls[-1], "gn_editor_capture_abort")

    def test_expired_session_is_reported_and_released(self):
        from blender_mcp.tools import review_image
        bridge = self.Bridge([{"expired": True, "draws_after_arm": 0, "match": {"matches": False}}])
        with self.assertRaises(review_image.ReviewImageError) as caught:
            review_image.run_deferred_capture(
                bridge, prepare_command="gn_editor_capture_prepare", status_command="gn_editor_capture_status",
                commit_command="gn_editor_capture_commit", abort_command="gn_editor_capture_abort",
                prepare_params={"group": "G"}, relative_path="previews/x.png", overwrite=False,
                wait_seconds=1.0, poll_interval=0.001)
        self.assertIn("expired", str(caught.exception))
        self.assertNotIn("gn_editor_capture_commit", bridge.calls)


# --------------------------------------------------------------------------- #
# bridge commands through the stub Blender API
# --------------------------------------------------------------------------- #
class LayoutTests(AddonCase):
    def test_layout_reports_structure_without_touching_the_view(self):
        before = [area.type for area in self.bpy.window.screen.areas]
        payload = self.command("gn_editor_layout")(group="GN_Test")
        nodes = {row["name"]: row for row in payload["nodes"]}
        self.assertEqual(payload["group"]["name"], "GN_Test")
        self.assertEqual(payload["counts"]["nodes"], 5)
        self.assertEqual(payload["counts"]["frames"], 1)
        self.assertEqual(payload["counts"]["reroutes"], 1)
        self.assertEqual(payload["counts"]["links"], 3)
        self.assertEqual(nodes["Process"]["parent"], "FrameA")
        self.assertEqual(nodes["Process"]["location_absolute"][0], -200.0)
        self.assertEqual(nodes["Process"]["location_absolute"][1], 300.0)
        self.assertTrue(nodes["Process"]["visible_in_editor"])
        self.assertTrue(nodes["Reroute"]["reroute"]["socket_type"])
        self.assertEqual(payload["links"][0]["from_node"], "Input")
        self.assertEqual(payload["links"][0]["from_socket"], "Geometry")
        self.assertIn("multi_input_sort_id", payload["links"][0])
        self.assertEqual(payload["resolution"]["group"], "GN_Test")
        self.assertIn(payload["read_support"]["multi_input_order"], ("NodeLink.multi_input_sort_id",
                                                                    "unavailable_in_this_build"))
        self.assertEqual([area.type for area in self.bpy.window.screen.areas], before)
        self.assertTrue(payload["changed_nothing"])

    def test_layout_with_object_and_modifier_resolution(self):
        obj = FakeObject("Chair", self.tree, mod_name="GN")
        self.bpy.data.objects.append(obj)
        payload = self.command("gn_editor_layout")(group="GN_Test", object="Chair", modifier="GN")
        self.assertEqual(payload["resolution"]["object"], "Chair")
        self.assertEqual(payload["resolution"]["modifier"], "GN")
        self.assertEqual(payload["modifier_users"][0]["object"], "Chair")

    def test_layout_errors_are_structured(self):
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_layout")(group="Missing")
        self.assertEqual(caught.exception.code, "node_group_not_found")
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_layout")(group="GN_Test", nodes=["Nope"])
        self.assertEqual(caught.exception.code, "node_not_found")
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_layout")(group="GN_Test", object="Chair")
        self.assertEqual(caught.exception.code, "object_not_found")

    def test_subset_bounds_include_parent_frames_and_flag_collapsed(self):
        frame = self.tree.nodes.get("FrameA")
        frame.shrink = True
        payload = self.command("gn_editor_layout")(group="GN_Test", nodes=["Process"])
        subset = payload["subset"]
        self.assertEqual(subset["requested"], ["Process"])
        self.assertIn("FrameA", subset["expanded_to_frames"])
        self.assertEqual(subset["collapsed"][0]["collapsed_frame"], "FrameA")
        self.assertIsNotNone(subset["bounds"])
        self.assertEqual(subset["bounds"]["size"][0], 180.0)


class PrepareCommitTests(AddonCase):
    def test_native_window_capture_uses_imbuf_instance_free(self):
        area=self.area
        area.x=area.y=0
        class Pixels:
            shape=(640,900,4)
            def cast(self, format):
                return memoryview(bytearray([20,40,60,255]*900*640))
        pixels=Pixels()
        flat=pixels.cast('B')
        flat[0]=99
        pixels.cast=lambda format:flat
        buffer=NS(file_type=None,free=lambda:setattr(buffer,'freed',True),freed=False)
        @contextmanager
        def with_buffer(**kwargs):
            yield memoryview(bytearray(len(flat)))
        buffer.with_buffer=with_buffer
        imbuf=NS(new=lambda size:buffer,write=lambda b,filepath:Path(filepath).write_bytes(png_bytes(900,640)))
        self.bpy.window.screenshot=lambda **kwargs:pixels
        target=self.root/'.runtime/native.png'
        with patch.dict(sys.modules,{'imbuf':imbuf}):
            result=self.common.screenshot_area(self.api,self.bpy.window,self.bpy.window.screen,
                                               area,area.regions[0],target)
        self.assertEqual(result['operator'],'bpy.types.Window.screenshot')
        self.assertTrue(buffer.freed)
        self.assertTrue(target.is_file())

    def test_rna_area_wrapper_identity_is_not_python_identity(self):
        prepared = self.prepare()
        area, space = self.editor_space()
        space.paint()
        proxy = NS(as_pointer=lambda:area.as_pointer())
        self.bpy.window.screen.areas = [proxy]
        result = self.command('gn_editor_capture_commit')(
            session=prepared['session'], relative_path='previews/wrapper.png')
        self.assertTrue(result['restored'])

    def test_prepare_converts_area_temporarily_and_restores_everything(self):
        selection_before = self.bpy.context.selected_objects
        prepared = self.prepare(fit="KEEP")
        area, space = self.editor_space()
        self.assertTrue(prepared["armed"])
        self.assertTrue(prepared["conversion"]["performed"])
        self.assertEqual(prepared["conversion"]["area_type_changed"]["from"], "VIEW_3D")
        self.assertEqual(area.type, "NODE_EDITOR")
        self.assertIs(space.node_tree, self.tree)
        self.assertEqual(prepared["display"]["verified"], True)
        self.assertFalse(prepared["display"]["displayed_tree_type"] == "ShaderNodeTree")
        self.assertEqual(len(space.handlers), 1)

        # the deferred step: Blender's main loop paints one frame
        space.paint()
        status = self.command("gn_editor_capture_status")(session=prepared["session"])
        self.assertGreaterEqual(status["draws_after_arm"], 1)
        self.assertTrue(status["match"]["matches"])
        self.assertTrue(status["match"]["checks"]["tree_identity"])

        result = self.command("gn_editor_capture_commit")(
            session=prepared["session"], relative_path="previews/editor.png")
        self.assertTrue((self.root / "Project" / "previews" / "editor.png").is_file())
        self.assertEqual(result["width"], 64)
        self.assertEqual(result["height"], 48)
        self.assertEqual(base64.b64decode(result["image_base64"])[:8], PNG_MAGIC)
        self.assertEqual(result["capture"]["operator"], "bpy.ops.screen.screenshot_area")
        self.assertEqual(result["draw_evidence"]["match"]["matches"], True)
        self.assertEqual(result["node_layout"]["counts"]["nodes"], 5)
        self.assertIn("whole editor area", " ".join(result["notes"]))
        # UI restored: area type, tree (cleared again), sidebar and selection
        self.assertEqual(area.type, "VIEW_3D")
        self.assertIsNone(space.node_tree)
        self.assertFalse(space.show_region_ui)
        self.assertEqual(len(space.handlers), 0)
        self.assertEqual(self.bpy.context.selected_objects, selection_before)
        self.assertTrue(result["restored"])

    def test_commit_without_a_draw_is_refused_and_restores(self):
        prepared = self.prepare()
        area, space = self.editor_space()
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(
                session=prepared["session"], relative_path="previews/stale.png")
        self.assertEqual(caught.exception.code, "stale_frame")
        self.assertFalse((self.root / "Project" / "previews" / "stale.png").exists())
        aborted = self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertTrue(aborted["aborted"])
        self.assertEqual(area.type, "VIEW_3D")
        self.assertIsNone(space.node_tree)
        self.assertEqual(len(space.handlers), 0)

    def test_wrong_tree_is_never_returned(self):
        prepared = self.prepare()
        _area, space = self.editor_space()
        space.node_tree = None  # something (a pin, another tree) took the editor over
        space.paint()
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(
                session=prepared["session"], relative_path="previews/wrong.png")
        self.assertEqual(caught.exception.code, "target_not_drawn")
        self.assertFalse((self.root / "Project" / "previews" / "wrong.png").exists())
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_redraw_timer_fast_path_can_confirm_the_draw(self):
        self.bpy.ops.wm.redraw_areas = True
        prepared = self.prepare()
        status = self.command("gn_editor_capture_status")(session=prepared["session"])
        self.assertGreaterEqual(status["draws_after_arm"], 1)
        self.assertTrue(status["match"]["matches"])
        self.assertTrue(prepared["redraw_timer"]["attempted"])
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_fit_selected_selects_the_subset_and_reports_collapsed_members(self):
        frame = self.tree.nodes.get("FrameA")
        frame.shrink = True
        prepared = self.prepare(fit="SELECTED", nodes=["Process", "Output"])
        selection = prepared["fit"]["selection"]
        self.assertEqual(sorted(selection["selected"]), ["FrameA", "Output"])
        self.assertEqual(selection["skipped"][0]["framed_by"], "FrameA")
        self.assertTrue(any("collapsed Frame" in warning for warning in prepared["warnings"]))
        self.assertEqual(prepared["fit"]["operator"], "bpy.ops.node.view_selected")
        self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertFalse(self.tree.nodes.get("Output").select)

    def test_fit_all_uses_view_all_and_sidebar_flag(self):
        prepared = self.prepare(fit="ALL", include_sidebar=False)
        _area, space = self.editor_space()
        self.assertEqual(prepared["fit"]["operator"], "bpy.ops.node.view_all")
        self.assertFalse(space.show_region_ui)
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_selected_fit_without_nodes_needs_a_selection(self):
        with self.assertRaises(Failure) as caught:
            self.prepare(fit="SELECTED")
        self.assertEqual(caught.exception.code, "empty_selection")

    def test_editor_that_cannot_show_the_tree_fails_and_restores(self):
        self.area.type = "NODE_EDITOR"
        space = self.editor_space()[1]
        self.area.type = "VIEW_3D"
        space.fail_tree_assignment = True
        with self.assertRaises(Failure) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "tree_not_displayed")
        self.assertEqual(self.area.type, "VIEW_3D")
        self.assertEqual(len(self.common._SESSIONS), 0)

    def test_explicit_area_index_of_another_type_is_restored(self):
        outliner = FakeArea("OUTLINER")
        self.bpy.window.screen.areas.append(outliner)
        index = len(self.bpy.window.screen.areas) - 1
        prepared = self.prepare(area_index=index)
        self.assertEqual(outliner.type, "NODE_EDITOR")
        self.command("gn_editor_capture_abort")(session=prepared["session"])
        self.assertEqual(outliner.type, "OUTLINER")

    def test_invalid_arguments(self):
        with self.assertRaises(Failure) as caught:
            self.prepare(fit="sideways")
        self.assertEqual(caught.exception.code, "invalid_argument")
        with self.assertRaises(Failure) as caught:
            self.prepare(window_index=7)
        self.assertEqual(caught.exception.code, "window_index_out_of_range")
        with self.assertRaises(Failure) as caught:
            self.prepare(area_index=99)
        self.assertEqual(caught.exception.code, "area_index_out_of_range")
        with self.assertRaises(Failure) as caught:
            self.prepare(include_sidebar="yes")
        self.assertEqual(caught.exception.code, "invalid_argument")
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_status")(session="nope")
        self.assertEqual(caught.exception.code, "unknown_session")

    def test_background_session_refuses_to_capture(self):
        self.bpy.app.background = True
        with self.assertRaises(Failure) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "gui_required")


class PublishSafetyTests(AddonCase):
    def _arm_and_paint(self, **kwargs):
        prepared = self.prepare(**kwargs)
        self.editor_space()[1].paint()
        return prepared

    def test_paths_outside_previews_are_refused(self):
        prepared = self._arm_and_paint()
        for bad in ("editor.png", "other/editor.png", "previews/../editor.png", "/tmp/editor.png",
                    "previews/editor.jpg"):
            with self.assertRaises(Failure) as caught:
                self.command("gn_editor_capture_commit")(session=prepared["session"], relative_path=bad)
            self.assertIn(caught.exception.code, ("invalid_path", "invalid_extension", "invalid_argument"))
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_existing_file_needs_overwrite_and_is_replaced_atomically(self):
        prepared = self._arm_and_paint()
        target = self.root / "Project" / "previews" / "keep.png"
        target.write_bytes(b"old")
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                     relative_path="previews/keep.png")
        self.assertEqual(caught.exception.code, "file_exists")
        self.assertEqual(target.read_bytes(), b"old")
        result = self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                          relative_path="previews/keep.png", overwrite=True)
        self.assertTrue(target.read_bytes().startswith(PNG_MAGIC))
        self.assertEqual(result["sha256"], __import__("hashlib").sha256(target.read_bytes()).hexdigest())
        self.assertEqual(result["mtime_ns"], target.stat().st_mtime_ns)

    def test_a_non_png_answer_is_rejected_and_nothing_is_published(self):
        def write_garbage(filepath, **_kwargs):
            Path(filepath).write_bytes(b"this is not a png")

        self.bpy.ops.screen.screenshot_area = FakeOperator(on_call=write_garbage)
        prepared = self._arm_and_paint()
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                     relative_path="previews/broken.png")
        self.assertEqual(caught.exception.code, "invalid_image")
        self.assertFalse((self.root / "Project" / "previews" / "broken.png").exists())
        leftovers = list((self.root / ".runtime" / "tmp").glob("*.png"))
        self.assertEqual(leftovers, [])
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_oversized_capture_is_refused(self):
        def write_big(filepath, **_kwargs):
            Path(filepath).write_bytes(self.common.PNG_MAGIC + b"\x00" * (self.common.MAX_PUBLISH_BYTES + 16))

        self.bpy.ops.screen.screenshot_area = FakeOperator(on_call=write_big)
        prepared = self._arm_and_paint()
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                     relative_path="previews/big.png")
        self.assertEqual(caught.exception.code, "invalid_image")
        self.command("gn_editor_capture_abort")(session=prepared["session"])

    def test_operator_that_writes_nothing_is_an_error(self):
        self.bpy.ops.screen.screenshot_area = FakeOperator(result=("CANCELLED",))
        prepared = self._arm_and_paint()
        with self.assertRaises(Failure) as caught:
            self.command("gn_editor_capture_commit")(session=prepared["session"],
                                                     relative_path="previews/none.png")
        self.assertEqual(caught.exception.code, "screenshot_failed")
        self.command("gn_editor_capture_abort")(session=prepared["session"])


# --------------------------------------------------------------------------- #
# live acceptance (project owner; never faked)
# --------------------------------------------------------------------------- #
async def live():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    checks = {}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=__import__("datetime").timedelta(seconds=120)) as client:
            await client.initialize()

            async def call(name, args=None):
                reply = await client.call_tool(name, args or {})
                content = list(reply.content)
                text = "\n".join(block.text for block in content if block.type == "text")
                if reply.isError:
                    return {"error": text}
                return {"blocks": [block.type for block in content], "text": text}

            health = json.loads((await call("blender_health"))["text"])
            assert health["blender_version_tuple"] == [5, 2, 1], health["blender_version_tuple"]
            groups = [row["name"] for row in health.get("node_groups", [])] if isinstance(
                health.get("node_groups"), list) else []
            group = groups[0] if groups else None
            session = "GNCaptureLive_" + uuid.uuid4().hex[:8]
            output = {"blender": health["blender_version_string"], "group": group, "checks": checks,
                      "scene": health["scene"]}
            try:
                if group:
                    reply = await call("blender_geometry_nodes_capture_editor", {
                        "group": group, "fit": "ALL", "include_sidebar": True,
                        "relative_path": "previews/%s.png" % session})
                    checks["mcp_image_content"] = (not reply.get("error")) and reply["blocks"][:2] == ["text", "image"]
                    if not reply.get("error"):
                        payload = json.loads(reply["text"])
                        checks["draw_evidence"] = bool(payload["draw_evidence"]["match"]["matches"])
                        checks["layout_present"] = payload["node_layout"]["counts"]["nodes"] >= 1
                        checks["ui_restored"] = bool(payload["restored"])
                output["ok"] = all(checks.values()) and bool(checks)
                assert output["ok"], checks
            finally:
                REPORT.parent.mkdir(parents=True, exist_ok=True)
                REPORT.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"ok": output["ok"], "checks": checks, "report": str(REPORT)}))


if __name__ == "__main__":
    if "--live" in sys.argv:
        asyncio.run(asyncio.wait_for(live(), timeout=240))
    else:
        unittest.main()
