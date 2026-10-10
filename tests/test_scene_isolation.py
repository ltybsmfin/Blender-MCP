"""Scene isolation + per-scene export: unit/safety checks and a live SDK acceptance runner.

Part 1 (always, no Blender/bridge): the five new MCP tools and their schemas, the
MCP-side path guard (rejected paths must never reach the bridge - asserted with a
recording stub), and the add-on's safety machinery under mocked failure injection
(container/staging names, explicit ``libraries.write`` arguments, the callable
``temp_data()`` factory, atomic no-replace publication, staged-manifest
verification including dependency-presence, the inverse ``user_map`` closure and
the guarded collection target).

Part 2 (``--live``): drives the *already running* Blender through the official MCP
stdio SDK only (named tools; never a raw socket, never an arbitrary ``bpy`` script),
and never starts or stops Blender.  It creates two uniquely prefixed case scenes,
one small object, a Geometry Nodes group, a shape key and a self-contained driver,
exports ``Project/算法复现/00_算法基础/_MCP验收_<unique>/isolated.blend``, inspects it
through MCP, proves the original scenes and the main-file path are unchanged,
exercises the rejection matrix (overwrite, path escape, missing scene, duplicate
scene name, external-object dependency) and switches back to the original scene.
Nothing is deleted; the token is read by the MCP server itself and never printed.

Run::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_scene_isolation.py
    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_scene_isolation.py --live

Reports: ``MCP/.runtime/scene_isolation_report.json`` and
``MCP/.runtime/logs/scene_isolation_<utc>.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
WORKSPACE = MCP_DIR.parent
RUNTIME = MCP_DIR / ".runtime"
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(MCP_DIR / "blender"))
sys.path.insert(0, str(TESTS_DIR))

NEW_TOOLS = (
    "blender_create_case_scene",
    "blender_switch_scene",
    "blender_list_scenes",
    "blender_save_scene",
    "blender_inspect_blend_file",
)

#: Unique case folder/file of the live acceptance run.
CASE_FOLDER_TEMPLATE = "_MCP验收_%s"
CASE_RELATIVE_DIR = "算法复现/00_算法基础/%s"
CALL_TIMEOUT = 60.0
SAVE_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# stub bpy so the self-contained add-on module can be imported without Blender
# --------------------------------------------------------------------------- #
def _install_bpy_stub() -> None:
    if "bpy" in sys.modules:
        return
    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")

    class _Operator:
        pass

    class _Panel:
        pass

    class _EmptyCollection:
        def get(self, name, default=None):
            return default

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def keys(self):
            return []

        def __getitem__(self, name):
            raise KeyError(name)

    bpy_types.Operator = _Operator
    bpy_types.Panel = _Panel
    bpy.types = bpy_types
    bpy.app = types.ModuleType("bpy.app")
    bpy.context = types.ModuleType("bpy.context")
    bpy_data = types.ModuleType("bpy.data")
    for collection in (
        "objects", "node_groups", "scenes", "collections", "meshes", "materials",
        "shape_keys", "worlds", "window_managers", "libraries",
    ):
        setattr(bpy_data, collection, _EmptyCollection())
    bpy.data = bpy_data
    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy_types


try:  # pragma: no cover - depends on the host
    import bpy  # noqa: F401

    _HAVE_REAL_BPY = True
except Exception:  # noqa: BLE001 - outside Blender the import always fails
    sys.modules.pop("bpy", None)
    sys.modules.pop("bpy.types", None)
    _install_bpy_stub()
    _HAVE_REAL_BPY = False

import blender_mcp_addon as addon  # noqa: E402  (needs the stub first)

from blender_mcp import audit, config  # noqa: E402
from blender_mcp.tools import build_server  # noqa: E402

TMP_ROOT = RUNTIME / "test_tmp" / "scene_isolation"


def _fresh_temp_dir(test) -> Path:
    """A private probe directory per test, removed again afterwards."""
    directory = TMP_ROOT / type(test).__name__ / test._testMethodName
    directory.mkdir(parents=True, exist_ok=True)

    def cleanup():
        for item in list(directory.glob("*")):
            try:
                if item.is_file():
                    item.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    test.addCleanup(cleanup)
    return directory


# --------------------------------------------------------------------------- #
# recording stub bridge (MCP-side validation tests, no Blender involved)
# --------------------------------------------------------------------------- #
class StubBridge:
    """Answers ``health`` (for the audit probe) and records every other command."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, command, params=None, timeout=None):
        self.calls.append({"command": command, "params": dict(params or {}), "timeout": timeout})
        if command == "health":
            return {"bridge": "stub", "audit_protocol": audit.ADDON_AUDIT_PROTOCOL, "commands": []}
        return {"stub": True, "command": command, "params": dict(params or {})}

    def close(self) -> None:
        pass

    def commands(self) -> list:
        return [entry["command"] for entry in self.calls if entry["command"] != "health"]

    def params_of(self, command: str) -> dict:
        for entry in self.calls:
            if entry["command"] == command:
                return entry["params"]
        return {}


# --------------------------------------------------------------------------- #
# part 1a: MCP tool surface / schema
# --------------------------------------------------------------------------- #
class ToolSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = build_server(profile="all")
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.mcp.list_tools())}

    def schema(self, name: str) -> dict:
        return self.tools[name].inputSchema

    def test_new_tools_are_registered_with_typed_schemas(self):
        missing = [name for name in NEW_TOOLS if name not in self.tools]
        self.assertEqual(missing, [], "missing MCP tools: %s" % missing)
        for name in NEW_TOOLS:
            lowered = name.lower()
            self.assertNotIn("eval", lowered)
            self.assertNotIn("exec", lowered)
            self.assertNotIn("python", lowered)

        create = self.schema("blender_create_case_scene")
        self.assertEqual(create["properties"]["name"]["type"], "string")
        self.assertEqual(create["properties"]["activate"]["default"], True)
        self.assertIn("name", create.get("required", ["name"]))

        switch = self.schema("blender_switch_scene")
        self.assertEqual(switch["properties"]["name"]["type"], "string")
        self.assertIn("name", switch.get("required", ["name"]))

        scenes = self.schema("blender_list_scenes")
        self.assertEqual(scenes["properties"]["include_objects"]["default"], False)

        save = self.schema("blender_save_scene")
        self.assertEqual(save["properties"]["scene"]["type"], "string")
        self.assertEqual(save["properties"]["relative_path"]["type"], "string")
        self.assertEqual(save["properties"]["overwrite"]["default"], False)
        self.assertEqual(save["properties"]["compress"]["default"], False)
        self.assertEqual(set(save.get("required", [])), {"scene", "relative_path"})

        inspect = self.schema("blender_inspect_blend_file")
        self.assertEqual(inspect["properties"]["relative_path"]["type"], "string")
        self.assertEqual(set(inspect.get("required", [])), {"relative_path"})

    def test_descriptions_document_the_isolation_contract(self):
        expected = {
            "blender_create_case_scene": ("empty", "auto-suffix", "activate"),
            "blender_switch_scene": ("window.scene", "verified"),
            "blender_list_scenes": ("object", "scene"),
            "blender_save_scene": ("libraries.write", "path_remap", "temp_data", "Project", "overwrite"),
            "blender_inspect_blend_file": ("Read-only", "temp_data"),
            "blender_create_mesh_object": ("collection_outside_scene", "shared", "ACTIVE scene"),
        }
        for name, needles in expected.items():
            description = self.tools[name].description or ""
            for needle in needles:
                self.assertIn(needle, description, "%s description must mention %r" % (name, needle))


# --------------------------------------------------------------------------- #
# part 1b: MCP-side validation happens before any bridge round trip
# --------------------------------------------------------------------------- #
def _structured(result) -> dict:
    """Normalise an in-process MCP tool result (content blocks or structured) to a dict."""
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


class McpPathGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = None
        cls.bridge = None

    def setUp(self):
        self.bridge = StubBridge()
        self.server = build_server(self.bridge, profile="all")
        self.work = _fresh_temp_dir(self)
        audit.configure(directory=RUNTIME / "scene_isolation_audit", source="mcp")

    def call(self, name, arguments):
        return asyncio.run(self.server.call_tool(name, arguments))

    def test_unsafe_paths_are_rejected_before_the_bridge(self):
        for bad in ("../escape.blend", "demo/../../escape.blend", "..\\escape.blend",
                    str(config.project_dir() / "absolute.blend"), "demo/notes.txt"):
            with self.assertRaises(Exception, msg=bad) as ctx:
                self.call("blender_save_scene", {"scene": "S", "relative_path": bad})
            self.assertIn("invalid_path", str(ctx.exception), "path %r" % bad)
        for bad in ("../escape.blend", "demo/notes.txt"):
            with self.assertRaises(Exception, msg=bad) as ctx:
                self.call("blender_inspect_blend_file", {"relative_path": bad})
            self.assertIn("invalid_path", str(ctx.exception), "path %r" % bad)
        self.assertEqual(self.bridge.commands(), [],
                         "a rejected path must never reach Blender: %s" % self.bridge.calls)

    def test_save_scene_rejects_existing_file_without_overwrite(self):
        target = self.work / "exists.blend"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"BLENDER-stub-payload")
        original = config.resolve_project_path
        config.resolve_project_path = lambda relative: target  # type: ignore[assignment]
        try:
            with self.assertRaises(Exception) as ctx:
                self.call("blender_save_scene", {"scene": "S", "relative_path": "x.blend"})
        finally:
            config.resolve_project_path = original  # type: ignore[assignment]
        self.assertIn("file_exists", str(ctx.exception))
        self.assertEqual(self.bridge.commands(), [], "the guard must fail before Blender is called")
        self.assertEqual(target.read_bytes(), b"BLENDER-stub-payload")

    def test_valid_requests_are_forwarded_typed(self):
        target = self.work / ("forward_%s.blend" % uuid.uuid4().hex[:8])
        original = config.resolve_project_path
        config.resolve_project_path = lambda relative: target  # type: ignore[assignment]
        try:
            payload = self.call(
                "blender_save_scene",
                {"scene": "Case_A", "relative_path": "case/out.blend", "overwrite": False, "compress": True},
            )
        finally:
            config.resolve_project_path = original  # type: ignore[assignment]
        self.assertEqual(self.bridge.commands(), ["save_scene"])
        forwarded = self.bridge.params_of("save_scene")
        self.assertEqual(forwarded["scene"], "Case_A")
        self.assertEqual(forwarded["relative_path"], "case/out.blend")
        self.assertIs(forwarded["overwrite"], False)
        self.assertIs(forwarded["compress"], True)
        self.assertIs(_structured(payload).get("stub"), True)

        payload = self.call("blender_inspect_blend_file", {"relative_path": "demo/isolated.blend"})
        self.assertEqual(self.bridge.params_of("inspect_blend_file")["relative_path"], "demo/isolated.blend")
        self.assertIs(_structured(payload).get("stub"), True)

    def test_scene_tools_reject_empty_names_before_the_bridge(self):
        for tool, arguments in (
            ("blender_create_case_scene", {"name": "  ", "activate": True}),
            ("blender_switch_scene", {"name": ""}),
            ("blender_save_scene", {"scene": " ", "relative_path": "demo/a.blend"}),
        ):
            with self.assertRaises(Exception, msg=tool) as ctx:
                self.call(tool, arguments)
            self.assertIn("invalid_argument", str(ctx.exception), tool)
        self.assertEqual(self.bridge.commands(), [])

    def test_case_scene_and_switch_forward_typed_arguments(self):
        self.call("blender_create_case_scene", {"name": "Case_A", "activate": False})
        self.call("blender_switch_scene", {"name": "Case_A"})
        self.call("blender_list_scenes", {"include_objects": True})
        self.assertEqual(self.bridge.commands(), ["create_case_scene", "switch_scene", "list_scenes"])
        self.assertIs(self.bridge.params_of("create_case_scene")["activate"], False)
        self.assertIs(self.bridge.params_of("list_scenes")["include_objects"], True)


# --------------------------------------------------------------------------- #
# part 1c: add-on safety machinery (mocked failure injection)
# --------------------------------------------------------------------------- #
class _FakeCollection:
    def __init__(self, name, children=(), objects=()):
        self.name = name
        self.children_recursive = list(children)
        self.all_objects = list(objects)
        self.objects = list(objects)


class _CollectionRegistry:
    """Minimal stand-in for ``bpy.data.collections`` (lookup + iteration)."""

    def __init__(self, mapping):
        self._mapping = dict(mapping)

    def get(self, name, default=None):
        return self._mapping.get(name, default)

    def __iter__(self):
        return iter(self._mapping.values())

    def __len__(self):
        return len(self._mapping)

    def keys(self):
        return list(self._mapping)


class _FakeViewLayer:
    def __init__(self, name):
        self.name = name


class _FakeWorld:
    def __init__(self, name):
        self.name = name


class _FakeScene:
    def __init__(self, name, collection, objects=(), world=None, view_layers=("ViewLayer",)):
        self.name = name
        self.collection = collection
        self.objects = list(objects)
        self.world = world
        self.view_layers = [_FakeViewLayer(layer) for layer in view_layers]
        self.frame_start = 1
        self.frame_end = 250
        self.frame_current = 1
        self.session_uid = 7


class _FakeID:
    def __init__(self, name, pointer):
        self.name = name
        self._pointer = pointer

    def as_pointer(self):
        return self._pointer


def _fake_object(name):
    return types.SimpleNamespace(name=name)


def _raise_os_error(*args, **kwargs):
    raise OSError("simulated filesystem refusal")


@contextmanager
def _patched(target, name, value):
    """Temporarily set an attribute on the (stub) ``bpy.data`` module."""
    sentinel = object()
    original = getattr(target, name, sentinel)
    setattr(target, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            try:
                delattr(target, name)
            except AttributeError:
                pass
        else:
            setattr(target, name, original)


class _FakeTempData:
    """Models ``bpy.data.temp_data`` as the callable context-manager factory it is.

    ``bpy.data.temp_data(*, filepath=None)`` returns a temporary ``BlendData`` that
    is freed when its ``with`` block exits; the real object has no ``.libraries``
    attribute of its own, so neither does this fake.
    """

    def __init__(self, data_from, failure=None):
        self.data_from = data_from
        self.failure = failure
        self.calls = 0
        self.entered = 0
        self.exited = 0
        self.load_entered = 0
        self.load_exited = 0
        self.factory_filepath_args: list = []
        self.paths: list[str] = []

    def __call__(self, *, filepath=None):
        self.calls += 1
        self.factory_filepath_args.append(filepath)
        if self.failure is not None:
            raise self.failure
        return _FakeTemporary(self)


class _FakeTemporary:
    def __init__(self, factory):
        self._factory = factory
        self.libraries = types.SimpleNamespace(load=self._load)

    def _load(self, path, **kwargs):
        self._factory.paths.append(path)
        return _FakeLoad(self._factory)

    def __enter__(self):
        self._factory.entered += 1
        return self

    def __exit__(self, *exc):
        self._factory.exited += 1
        return False


class _FakeLoad:
    def __init__(self, factory):
        self._factory = factory

    def __enter__(self):
        self._factory.load_entered += 1
        return (self._factory.data_from, types.SimpleNamespace())

    def __exit__(self, *exc):
        self._factory.load_exited += 1
        return False


class AddonSafetyMachineryTests(unittest.TestCase):
    """The parts of the export pipeline that decide whether a file may be published."""

    def setUp(self):
        self.work = _fresh_temp_dir(self)

    # -- staged-file header / staging names ------------------------------- #
    def test_blend_container_and_stage_path_naming(self):
        cases = {
            b"BLENDER-v520" + b"\x00" * 32: "blend",
            b"\x1f\x8b\x08\x00" + b"\x00" * 32: "gzip",
            b"\x28\xb5\x2f\xfd" + b"\x00" * 32: "zstd",
            b"not-a-blend-file" + b"\x00" * 32: None,
        }
        for index, (content, expected) in enumerate(cases.items()):
            path = self.work / ("container_%d.blend" % index)
            path.write_bytes(content)
            self.assertEqual(addon._blend_container(path)["container"], expected, content[:8])
        target = self.work / "isolated.blend"
        first = addon._stage_path(target)
        second = addon._stage_path(target)
        self.assertEqual(first.parent, target.parent)
        self.assertNotEqual(first, target)
        self.assertNotEqual(first, second, "two stagings must not collide")
        self.assertTrue(addon._is_own_stage(first))
        self.assertFalse(addon._is_own_stage(target))

    def test_write_scene_blend_passes_explicit_arguments_and_fails_loud(self):
        scene = _FakeScene("Case_A", _FakeCollection("Scene"))
        stage = self.work / ("fault_%s.blend" % uuid.uuid4().hex[:8])
        calls: list[dict] = []
        payload = b"BLENDER-v520" + b"\x00" * (addon.MIN_STAGED_BLEND_BYTES + 8)

        def write_ok(filepath, datablocks, **kwargs):
            calls.append({"filepath": filepath, "datablocks": datablocks, "kwargs": kwargs})
            Path(filepath).write_bytes(payload)

        try:
            with _patched(addon.bpy.data, "libraries", types.SimpleNamespace(write=write_ok)):
                signature = addon._write_scene_blend(scene, stage, True)
            self.assertEqual(signature["container"], "blend")
            self.assertEqual(signature["bytes"], len(payload))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["filepath"], str(stage))
            self.assertEqual(calls[0]["datablocks"], {scene})
            self.assertEqual(
                calls[0]["kwargs"],
                {"fake_user": False, "compress": True, "path_remap": "ABSOLUTE"},
                "path_remap/fake_user/compress must be explicit, never left to defaults",
            )

            # a writer that claims success but writes nothing
            stage.unlink(missing_ok=True)
            with _patched(addon.bpy.data, "libraries", types.SimpleNamespace(write=lambda *a, **k: None)):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._write_scene_blend(scene, stage, False)
                self.assertEqual(ctx.exception.code, "scene_export_failed")

            # a garbage file is refused by the container check
            with _patched(addon.bpy.data, "libraries", types.SimpleNamespace(
                    write=lambda filepath, datablocks, **kwargs: Path(filepath).write_bytes(b"garbage" * 64))):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._write_scene_blend(scene, stage, False)
                self.assertEqual(ctx.exception.code, "scene_export_failed")

            # an API that rejects the explicit arguments is reported, never silently ignored
            def write_type_error(*args, **kwargs):
                raise TypeError("write() got an unexpected keyword argument 'path_remap'")

            with _patched(addon.bpy.data, "libraries", types.SimpleNamespace(write=write_type_error)):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._write_scene_blend(scene, stage, True)
                self.assertEqual(ctx.exception.code, "api_unsupported")
        finally:
            addon._cleanup_stage(stage)

    def test_temp_blend_metadata_uses_the_context_manager_factory(self):
        stage = self.work / "staged.blend"
        factory = _FakeTempData(types.SimpleNamespace(scenes=["Case_A"], objects=["o"]))
        with _patched(addon.bpy.data, "temp_data", factory):
            inventories = addon._temp_blend_metadata(stage)
        self.assertEqual(inventories, {"scenes": ["Case_A"], "objects": ["o"]})
        self.assertEqual(factory.calls, 1, "temp_data must be called as a factory")
        self.assertEqual(factory.entered, 1, "the factory result must be entered as a context")
        self.assertEqual(factory.exited, 1, "the temporary BlendData must be released")
        self.assertEqual(factory.load_entered, 1)
        self.assertEqual(factory.load_exited, 1)
        self.assertEqual(factory.paths, [str(stage)], "libraries.load receives the file path")

        # a bare object with a .libraries attribute is NOT the documented API
        with _patched(addon.bpy.data, "temp_data", types.SimpleNamespace(
                libraries=types.SimpleNamespace(load=lambda *a, **k: None))):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._temp_blend_metadata(stage)
        self.assertEqual(ctx.exception.code, "api_unsupported")

        # a factory whose context fails is a recoverable CommandError
        with _patched(addon.bpy.data, "temp_data", _FakeTempData(None, failure=RuntimeError("boom"))):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._temp_blend_metadata(stage)
        self.assertEqual(ctx.exception.code, "blend_inspect_failed")

    # -- exclusive publication -------------------------------------------- #
    def test_publish_is_atomic_and_never_leaves_a_partial_file(self):
        # 1. a fresh target is created atomically
        stage = self.work / ("%spublish.blend" % addon._STAGE_PREFIX)
        target = self.work / ("published_%s.blend" % uuid.uuid4().hex[:8])
        stage.write_bytes(b"BLENDER-payload")
        try:
            self.assertIn(addon._publish_stage(stage, target, False), ("exclusive_link", "exclusive_rename"))
            self.assertEqual(target.read_bytes(), b"BLENDER-payload")
            addon._cleanup_stage(stage)
            target.unlink()

            # 2. a target that appeared meanwhile is never replaced
            stage.write_bytes(b"BLENDER-new")
            target.write_bytes(b"BLENDER-old")
            with self.assertRaises(addon.CommandError) as ctx:
                addon._publish_stage(stage, target, False)
            self.assertEqual(ctx.exception.code, "file_exists")
            self.assertEqual(target.read_bytes(), b"BLENDER-old", "the existing file must survive")
            self.assertTrue(stage.exists(), "the caller cleans its own staging file")

            # 3. no atomic primitive at all: refuse, and leave no partial file behind
            with _patched(os, "link", _raise_os_error), _patched(os, "name", "posix"):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._publish_stage(stage, self.work / "never.blend", False)
            self.assertEqual(ctx.exception.code, "publish_unsupported")
            self.assertTrue(stage.exists(), "the staging file must survive a refused publish")
            self.assertFalse((self.work / "never.blend").exists(), "no partial file may be left behind")
            with _patched(os, "link", _raise_os_error), _patched(os, "rename", _raise_os_error):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._publish_stage(stage, self.work / "never_win.blend", False)
            self.assertEqual(ctx.exception.code, "publish_unsupported")
            self.assertFalse((self.work / "never_win.blend").exists())

            # 4. overwrite replaces atomically
            self.assertEqual(addon._publish_stage(stage, target, True), "replace")
            self.assertEqual(target.read_bytes(), b"BLENDER-new")
            self.assertFalse(stage.exists(), "os.replace moved the staging file")
        finally:
            addon._cleanup_stage(stage)
            target.unlink(missing_ok=True)

    def test_cleanup_only_touches_own_staging_files(self):
        foreign = self.work / "keep_me.blend"
        foreign.write_bytes(b"BLENDER-keep")
        stage = addon._stage_path(self.work / "x.blend")
        stage.write_bytes(b"BLENDER-stage")
        addon._cleanup_stage(foreign)
        addon._cleanup_stage(stage)
        self.assertTrue(foreign.exists(), "cleanup must never delete a non-staging file")
        self.assertFalse(stage.exists())

    # -- staged-file verification ----------------------------------------- #
    def _allowed(self, scene_name="Case_A", objects=("Case_A_object",)):
        manifest = {"scenes:%s" % scene_name: {"type": "scenes", "name": scene_name}}
        for name in objects:
            manifest["objects:%s" % name] = {"type": "objects", "name": name}
        manifest["meshes:%s" % objects[0]] = {"type": "meshes", "name": objects[0]}
        manifest["collections:%s" % scene_name] = {"type": "collections", "name": scene_name}
        manifest["node_groups:%s_gn" % scene_name] = {"type": "node_groups", "name": "%s_gn" % scene_name}
        return {
            "scene": scene_name,
            "objects": sorted(objects),
            "collections": [scene_name],
            "closure_size": len(manifest),
            "manifest": manifest,
            # the scene's own master collection is owned by the scene, not a linkable ID
            "embedded": ["collections:%s" % scene_name],
        }

    def _staged_ok(self):
        return {
            "scenes": ["Case_A"],
            "objects": ["Case_A_object"],
            "meshes": ["Case_A_object"],
            "collections": ["Case_A"],
            "node_groups": ["Case_A_gn"],
        }

    def test_verify_staged_accepts_the_proven_manifest(self):
        scene = _FakeScene("Case_A", _FakeCollection("Scene"))
        verdict = addon._verify_staged(self._staged_ok(), scene, self._allowed())
        self.assertEqual(verdict["scenes"], ["Case_A"])
        self.assertEqual(verdict["unexpected"], [])
        self.assertEqual(verdict["missing"], [])
        self.assertEqual(verdict["embedded"], {"collections": ["Case_A"]})
        self.assertEqual(verdict["unmapped"], [])

    def test_verify_staged_rejects_scene_object_and_unproven_mismatches(self):
        scene = _FakeScene("Case_A", _FakeCollection("Scene"))
        with self.assertRaises(addon.CommandError) as ctx:
            addon._verify_staged({"scenes": ["OldCase"], "objects": ["Case_A_object"]}, scene, self._allowed())
        self.assertEqual(ctx.exception.code, "staged_scene_mismatch")
        with self.assertRaises(addon.CommandError) as ctx:
            addon._verify_staged({"scenes": ["Case_A"], "objects": []}, scene, self._allowed())
        self.assertEqual(ctx.exception.code, "staged_objects_missing")
        with self.assertRaises(addon.CommandError) as ctx:
            addon._verify_staged(
                {"scenes": ["Case_A"], "objects": ["Case_A_object", "OldCase_object"]}, scene, self._allowed())
        self.assertEqual(ctx.exception.code, "staged_objects_unexpected")
        staged = self._staged_ok() | {"actions": ["OldCaseAction"]}
        with self.assertRaises(addon.CommandError) as ctx:
            addon._verify_staged(staged, scene, self._allowed())
        self.assertEqual(ctx.exception.code, "unexpected_datablock")

    def test_verify_staged_requires_exposed_dependencies_and_reports_embedded(self):
        scene = _FakeScene("Case_A", _FakeCollection("Scene"))
        allowed = self._allowed()
        # a type the metadata route cannot expose (embedded) and an unmappable ID are
        # reported honestly instead of being silently accepted or hard-required
        allowed["manifest"]["shape_keys:Case_A_key"] = {"type": "shape_keys", "name": "Case_A_key"}
        allowed["manifest"]["unknown:?mystery"] = {"type": "unknown", "name": "?mystery"}
        verdict = addon._verify_staged(self._staged_ok(), scene, allowed)
        self.assertEqual(verdict["embedded"], {"collections": ["Case_A"], "shape_keys": ["Case_A_key"]})
        self.assertEqual(verdict["embedded_count"], 2)
        self.assertEqual(verdict["unmapped"], ["unknown:?mystery"])
        # the metadata route exposes node_groups, so a lost group is a hard failure
        lost = self._staged_ok() | {"node_groups": []}
        with self.assertRaises(addon.CommandError) as ctx:
            addon._verify_staged(lost, scene, allowed)
        self.assertEqual(ctx.exception.code, "dependency_missing")
        self.assertIn("node_groups:Case_A_gn", ctx.exception.details["missing_dependencies"])

    # -- inverse user_map dependency closure ------------------------------ #
    def test_dependency_closure_includes_roots_and_inverts_user_map(self):
        scene = _FakeID("Case_A", 1)
        collection = _FakeID("Case_A_collection", 2)
        obj = _FakeID("Case_A_object", 3)
        mesh = _FakeID("Case_A_mesh", 4)
        foreign = _FakeID("OldCase_object", 5)
        relations = {
            collection: {scene},                      # the scene uses the collection
            obj: {collection},                        # the collection uses the object
            mesh: {obj},                              # the object uses its mesh
            foreign: {_FakeID("OldCase", 6)},         # unrelated branch
        }
        with _patched(addon.bpy.data, "user_map", lambda: relations):
            closure = addon._dependency_closure([scene])
        names = sorted(block.name for block in closure)
        self.assertEqual(
            names, ["Case_A", "Case_A_collection", "Case_A_mesh", "Case_A_object"],
            "the root scene itself belongs to the closure (it is written too)",
        )
        self.assertNotIn("OldCase_object", names)

    def test_dependency_closure_refuses_an_unusable_user_map(self):
        with _patched(addon.bpy.data, "user_map", lambda: ["not", "a", "mapping"]):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._dependency_closure([_FakeID("Case_A", 1)])
        self.assertEqual(ctx.exception.code, "dependency_guard_unavailable")

    # -- guarded collection creation -------------------------------------- #
    def test_guarded_collection_target_allows_only_the_active_scene(self):
        child = _FakeCollection("Case_child")
        master = _FakeCollection("Case_master", children=[child])
        scene = _FakeScene("Case_A", master)
        registry = _CollectionRegistry({"Case_master": master, "Case_child": child})
        with _patched(addon.bpy.data, "collections", registry), \
                _patched(addon.bpy.data, "scenes", [scene]):
            self.assertIs(addon._guarded_collection_target(scene, "Case_child"), child)
            self.assertIs(addon._guarded_collection_target(scene, "Case_master"), master)

    def test_guarded_collection_target_rejects_foreign_shared_and_unknown(self):
        master = _FakeCollection("Case_master")
        scene = _FakeScene("Case_A", master)
        foreign = _FakeCollection("OldCase_collection")
        shared = _FakeCollection("Shared_collection")
        other = _FakeScene("OldCase", _FakeCollection("OldCase_master", children=[shared]))
        # a collection that belongs to another scene only
        with _patched(addon.bpy.data, "collections", _CollectionRegistry({foreign.name: foreign})), \
                _patched(addon.bpy.data, "scenes", [scene, other]):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._guarded_collection_target(scene, foreign.name)
        self.assertEqual(ctx.exception.code, "collection_outside_scene")
        self.assertEqual(ctx.exception.details["collection"], "OldCase_collection")
        # a collection shared between the active scene and another one
        linked_master = _FakeCollection("Case_master", children=[shared])
        scene_with_shared = _FakeScene("Case_A", linked_master)
        with _patched(addon.bpy.data, "collections", _CollectionRegistry({shared.name: shared})), \
                _patched(addon.bpy.data, "scenes", [scene_with_shared, other]):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._guarded_collection_target(scene_with_shared, shared.name)
        self.assertEqual(ctx.exception.code, "collection_shared_across_scenes")
        self.assertEqual(ctx.exception.details["foreign_scenes"], ["OldCase"])
        # an unknown collection and a missing active scene
        with _patched(addon.bpy.data, "collections", _CollectionRegistry({})):
            with self.assertRaises(addon.CommandError) as ctx:
                addon._guarded_collection_target(scene, "nope")
            self.assertEqual(ctx.exception.code, "collection_not_found")
            with _patched(addon.bpy.data, "collections", _CollectionRegistry({master.name: master})):
                with self.assertRaises(addon.CommandError) as ctx:
                    addon._guarded_collection_target(None, "Case_master")
                self.assertEqual(ctx.exception.code, "context_unavailable")

    # -- activation proof / rollback reporting ----------------------------- #
    def test_activate_scene_fails_closed_and_reports_rollback(self):
        previous = _FakeScene("Old", _FakeCollection("Old_master"))
        target = _FakeScene("Case_A", _FakeCollection("Case_master"))

        class _Window:
            def __init__(self, scene, keep="all"):
                self._scene = scene
                self._keep = keep
                self._changes = 0

            @property
            def scene(self):
                return self._scene

            @scene.setter
            def scene(self, value):
                self._changes += 1
                if self._keep == "all" or (self._keep == "first" and self._changes == 1):
                    self._scene = value

        def activate(window, context_scene):
            with _patched(addon.bpy.context, "window", window), \
                    _patched(addon.bpy.context, "scene", context_scene), \
                    _patched(addon.bpy.context, "view_layer", types.SimpleNamespace(update=lambda: None)), \
                    _patched(addon.bpy.data, "window_managers",
                             [types.SimpleNamespace(windows=[window])]), \
                    _patched(addon.bpy.data, "scenes", [previous, target]):
                return addon._activate_scene(target)

        # 1. no context scene at all: fail closed and roll the switch back
        window = _Window(previous)
        with self.assertRaises(addon.CommandError) as ctx:
            activate(window, None)
        self.assertEqual(ctx.exception.code, "scene_switch_unverified")
        self.assertTrue(ctx.exception.details["rollback"]["restored"])
        self.assertIs(window.scene, previous)

        # 2a. the window never kept the assignment: it is still on the old scene
        window = _Window(previous, keep="none")
        with self.assertRaises(addon.CommandError) as ctx:
            activate(window, previous)
        self.assertEqual(ctx.exception.code, "scene_switch_failed")
        self.assertTrue(ctx.exception.details["rollback"]["restored"])
        self.assertIs(window.scene, previous)

        # 2b. the switch was kept but the rollback is refused: never claim "restored"
        window = _Window(previous, keep="first")
        with self.assertRaises(addon.CommandError) as ctx:
            activate(window, previous)
        self.assertEqual(ctx.exception.code, "scene_switch_unverified")
        self.assertFalse(ctx.exception.details["rollback"]["restored"],
                         "a failed rollback must not be reported as restored")
        self.assertIn("FAILED", str(ctx.exception))
        self.assertIs(window.scene, target, "the window really is stuck on the target scene")

        # 3. context stays on the old scene: the switch is rolled back
        window = _Window(previous)
        with self.assertRaises(addon.CommandError) as ctx:
            activate(window, previous)
        self.assertEqual(ctx.exception.code, "scene_switch_unverified")
        self.assertTrue(ctx.exception.details["rollback"]["restored"])
        self.assertIs(window.scene, previous)

        # 4. proven switch
        report, used_window, used_previous = activate(_Window(previous), target)
        self.assertIs(used_window.scene, target)
        self.assertIs(used_previous, previous)
        self.assertTrue(report["changed"])
        self.assertTrue(report["context_followed"])
        self.assertEqual(report["verified_by"], "window+context")
        self.assertEqual(report["previous_scene"], "Old")

        # 5. already current, context proven
        report, _window, _previous = activate(_Window(target), target)
        self.assertFalse(report["changed"])
        self.assertTrue(report["context_followed"])
        # 6. already current but no context scene: fail closed
        with self.assertRaises(addon.CommandError) as ctx:
            activate(_Window(target), None)
        self.assertEqual(ctx.exception.code, "context_unavailable")

    # -- emptiness / roster proofs ---------------------------------------- #
    def test_scene_emptiness_and_roster(self):
        master_a = _FakeCollection("A_master")
        master_b = _FakeCollection("B_master", children=[_FakeCollection("B_child")],
                                   objects=[_fake_object("B_object")])
        scene_a = _FakeScene("A", master_a)
        scene_b = _FakeScene("B", master_b, objects=[_fake_object("B_object")],
                             world=_FakeWorld("World"))
        self.assertEqual(
            addon._scene_emptiness(scene_a),
            {"objects": 0, "object_names": [], "collection_objects": 0, "child_collections": 0,
             "world": None, "view_layers": 1, "empty": True},
        )
        self.assertFalse(addon._scene_emptiness(scene_b)["empty"])
        with _patched(addon.bpy.data, "scenes", [scene_a, scene_b]):
            roster = addon._scene_roster()
        self.assertEqual(sorted(roster), ["A", "B"])
        self.assertEqual(roster["B"]["objects"], ["B_object"])
        self.assertEqual(roster["B"]["child_collections"], ["B_child"])
        self.assertEqual(roster["A"]["world"], None)
        self.assertEqual(roster["B"]["world"], "World")

    def test_stable_scene_id_prefers_session_uid(self):
        scene = _FakeScene("A", _FakeCollection("A_master"))
        self.assertEqual(addon._scene_stable_id(scene), {"session_uid": 7, "as_pointer": None,
                                                         "source": "session_uid"})


# --------------------------------------------------------------------------- #
# part 2: live acceptance through the official MCP stdio SDK
# --------------------------------------------------------------------------- #
LIVE_RESULTS: list[dict] = []


def record(name: str, ok: bool, details=None) -> bool:
    entry = {"name": name, "ok": bool(ok)}
    if details is not None:
        entry["details"] = details
    LIVE_RESULTS.append(entry)
    print("SCENEISO::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str)))
    return bool(ok)


def check(name: str, condition, details=None) -> bool:
    return record(name, bool(condition), details)


class LiveUnavailable(RuntimeError):
    """No Blender bridge is answering; the runner never starts Blender itself."""


class AcceptanceAborted(RuntimeError):
    """A critical early step failed: stop instead of mutating more (possibly old) scene data."""


def _result_text(result) -> str:
    parts = []
    structured = getattr(result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, default=str))
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


async def _call(session, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT):
    return await asyncio.wait_for(session.call_tool(tool, arguments), timeout=timeout)


async def _expect_ok(session, label: str, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT):
    try:
        result = await _call(session, tool, arguments, timeout)
    except Exception as exc:  # noqa: BLE001 - a raised error is still a failed call
        text = "%s: %s" % (type(exc).__name__, exc)
        record(label, False, {"raised": text[:400]})
        return False, {"_error": text}
    if getattr(result, "isError", False):
        text = _result_text(result)
        record(label, False, {"error": text[:400]})
        return False, {"_error": text}
    data = _payload(result)
    record(label, True, {"keys": sorted(data)[:10]})
    return True, data


async def _expect_error(session, label: str, tool: str, arguments: dict,
                        needle: str | None = None, timeout: float = CALL_TIMEOUT):
    try:
        result = await _call(session, tool, arguments, timeout)
    except Exception as exc:  # noqa: BLE001
        text = "%s: %s" % (type(exc).__name__, exc)
        return record(label, needle is None or needle in text, {"raised": text[:300]}), text
    text = _result_text(result)
    if not getattr(result, "isError", False):
        return record(label, False, {"unexpected_success": text[:300]}), text
    return record(label, needle is None or needle in text, {"error_text": text[:300]}), text


def _inventory(scene_listing: dict) -> dict:
    return {
        row["name"]: sorted(row.get("object_names", []))
        for row in scene_listing.get("scenes", [])
    }


def _env_for_server() -> dict:
    env = dict(os.environ)
    env.setdefault("BLENDER_MCP_TIMEOUT", "60")
    return env


def _leaf_exceptions(exc: BaseException) -> list:
    """Leaves of a (possibly nested) exception group."""
    if isinstance(exc, BaseExceptionGroup):
        leaves = []
        for item in exc.exceptions:
            leaves.extend(_leaf_exceptions(item))
        return leaves
    return [exc]


async def run_live_acceptance() -> dict:
    """Drive the running Blender through the MCP stdio SDK. Never launches Blender.

    ``anyio`` wraps anything raised inside the stdio transport's task group into an
    ``ExceptionGroup``, so the runner's own abort signals are unwrapped again here
    to keep the reported failure readable.
    """
    try:
        return await _run_live_session()
    except BaseExceptionGroup as group:  # noqa: BLE001 - transport-level wrapper
        for leaf in _leaf_exceptions(group):
            if isinstance(leaf, (LiveUnavailable, AcceptanceAborted)):
                raise leaf from None
        raise


async def _run_live_session() -> dict:
    from stdio_pipe_transport import mcp_stdio_session

    python = MCP_DIR / ".venv" / "Scripts" / "python.exe"
    server_py = MCP_DIR / "server.py"
    log_dir = RUNTIME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    unique = uuid.uuid4().hex[:8]
    prefix = "mcpiso_%s" % unique
    case_a = "%s_A" % prefix
    case_b = "%s_B" % prefix
    object_a = "%s_obj" % prefix
    object_b = "%s_obj_ext" % prefix
    node_group = "%s_gn" % prefix
    shape_key = "%s_key" % prefix
    relative_dir = CASE_RELATIVE_DIR % (CASE_FOLDER_TEMPLATE % prefix)
    relative_blend = "%s/isolated.blend" % relative_dir
    relative_external = "%s/isolated_external.blend" % relative_dir
    context: dict = {
        "prefix": prefix,
        "case_scenes": [case_a, case_b],
        "relative_blend": relative_blend,
        "relative_external": relative_external,
        "started_at": time.time(),
    }

    async with mcp_stdio_session(
        command=python,
        args=[str(server_py)],
        env=_env_for_server(),
        stderr_path=log_dir / "scene_isolation_server_stderr.log",
    ) as (session, _process):
        listed = {tool.name for tool in (await session.list_tools()).tools}
        record("live_new_tools_listed", set(NEW_TOOLS).issubset(listed),
               {"missing": sorted(set(NEW_TOOLS) - listed), "count": len(listed)})

        ok, health = await _expect_ok(session, "live_health", "blender_health", {})
        if not ok:
            text = str(health.get("_error", ""))
            if any(marker in text for marker in
                   ("bridge_unavailable", "cannot connect", "bridge_timeout", "START_HINT")):
                raise LiveUnavailable(
                    "no Blender bridge answered - this runner never starts Blender. Detail: %s"
                    % text[:240]
                )
            raise LiveUnavailable("blender_health failed: %s" % text[:300])
        original_scene = health.get("scene")
        filepath = health.get("blend_filepath")
        context.update({
            "blender_version": health.get("blender_version"),
            "background": health.get("background"),
            "original_scene": original_scene,
            "blend_filepath": filepath,
            "objects_before": health.get("objects"),
        })
        check("live_health_scene_and_filepath", bool(original_scene) and isinstance(filepath, str),
              {"scene": original_scene, "blend_filepath": filepath,
               "blender_version": health.get("blender_version"), "background": health.get("background")})

        # ---------------------------------------------------------------- baseline
        _, baseline_scenes = await _expect_ok(
            session, "live_list_scenes_baseline", "blender_list_scenes", {"include_objects": True})
        _, baseline_summary = await _expect_ok(
            session, "live_scene_summary_baseline", "blender_scene_summary", {"include_objects": False})
        baseline_inventory = _inventory(baseline_scenes)
        context["baseline_scenes"] = baseline_inventory
        context["baseline_collections"] = sorted(baseline_summary.get("collections", []))
        check("live_baseline_has_original_scene", original_scene in baseline_inventory,
              {"scenes": sorted(baseline_inventory)})
        if original_scene not in baseline_inventory:
            # Without a baseline the "originals unchanged" proof is impossible, so
            # nothing may be created.
            raise AcceptanceAborted(
                "blender_list_scenes did not report the original scene %r, so the run stopped before "
                "creating anything" % original_scene
            )

        # ------------------------------------------------- 1. create case scene A
        ok_a, created_a = await _expect_ok(
            session, "live_create_case_scene_A", "blender_create_case_scene",
            {"name": case_a, "activate": True})
        if not ok_a:
            # Nothing may be created before a verified switch: continuing would put
            # case objects into whatever scene is actually active.
            raise AcceptanceAborted(
                "case scene A could not be created and activated, so the run stopped before touching any "
                "scene data: %s" % str(created_a.get("_error", ""))[:240]
            )
        check("case_scene_A_is_empty",
              created_a.get("empty", {}).get("empty") is True
              and created_a.get("empty", {}).get("objects") == 0
              and created_a.get("empty", {}).get("world") is None,
              created_a.get("empty"))
        check("case_scene_A_reports_previous_and_current",
              created_a.get("previous_scene") == original_scene and created_a.get("current_scene") == case_a,
              {"previous": created_a.get("previous_scene"), "current": created_a.get("current_scene")})
        check("case_scene_A_has_stable_id",
              bool(created_a.get("stable_id", {}).get("as_pointer"))
              or created_a.get("stable_id", {}).get("session_uid") is not None,
              created_a.get("stable_id"))
        check("case_scene_A_existing_scenes_unchanged",
              created_a.get("existing_scenes_unchanged") is True
              and created_a.get("changed_scenes") == []
              and case_a not in created_a.get("existing_scenes", []),
              {"existing": created_a.get("existing_scenes"), "changed": created_a.get("changed_scenes")})
        check("case_scene_A_activation_verified",
              (created_a.get("activation") or {}).get("changed") is True
              and (created_a.get("activation") or {}).get("active_scene") == case_a,
              created_a.get("activation"))

        # ------------------------------------------------- 2. one small mesh object
        ok_object, object_payload = await _expect_ok(
            session, "live_create_case_object", "blender_create_mesh_object",
            {"name": object_a,
             "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
             "faces": [[0, 1, 2, 3]],
             "location": [0.0, 0.0, 0.5]})
        if not ok_object:
            raise AcceptanceAborted(
                "the case object could not be created in scene %r, so the run stopped: %s"
                % (case_a, str(object_payload.get("_error", ""))[:240])
            )
        check("case_object_created_in_case_scene",
              object_payload.get("created") == object_a and object_payload.get("vertices") == 4,
              object_payload)

        # ------------------------------- 3. guarded collection creation (isolation)
        if context["baseline_collections"]:
            foreign = context["baseline_collections"][0]
            await _expect_error(
                session, "live_collection_guard_rejects_foreign_collection",
                "blender_create_mesh_object",
                {"name": "%s_guard_probe" % prefix,
                 "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                 "faces": [[0, 1, 2]],
                 "collection": foreign},
                needle="collection_outside_scene")
            context["guard_collection"] = foreign
        else:
            record("live_collection_guard_rejects_foreign_collection", True,
                   {"skipped": "the loaded file has no pre-existing collection to probe with"})

        # ----------------------------------------------- 4. GN + shape key + driver
        _, group_payload = await _expect_ok(
            session, "live_create_node_group", "blender_create_node_group",
            {"name": node_group,
             "interface": [{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "INPUT"},
                           {"name": "Offset", "socket_type": "NodeSocketVector",
                            "default_value": [0.0, 0.0, 0.0]}]})
        check("node_group_created", group_payload.get("group", {}).get("name") == node_group,
              {"group": group_payload.get("group", {}).get("name"), "created": group_payload.get("created")})
        await _expect_ok(session, "live_describe_transform_sockets", "blender_describe_node_type",
                         {"node_type": "GeometryNodeTransform"})
        await _expect_ok(session, "live_build_working_geometry_graph", "blender_batch_graph_edit",
                         {"group": node_group, "operations": [
                             {"op": "create_node", "node_type": "GeometryNodeTransform", "name": "Offset Geometry"},
                             {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Geometry",
                              "to_node": "Offset Geometry", "to_socket": "Geometry"},
                             {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Offset",
                              "to_node": "Offset Geometry", "to_socket": "Translation"},
                             {"op": "link_sockets", "from_node": "Offset Geometry", "from_socket": "Geometry",
                              "to_node": "Group Output", "to_socket": "Geometry"}]})
        _, attached = await _expect_ok(
            session, "live_attach_gn_modifier", "blender_attach_geometry_nodes_modifier",
            {"object": object_a, "group": node_group, "parameters": {"Offset": [0.25, 0.0, 0.0]}})
        check("gn_modifier_attached",
              attached.get("object") == object_a and attached.get("node_group") == node_group,
              {"object": attached.get("object"), "modifier": attached.get("modifier"),
               "node_group": attached.get("node_group")})
        check("gn_graph_produces_real_geometry",
              attached.get("evaluated", {}).get("evaluated", {}).get("vertices") == object_payload.get("vertices")
              and int(attached.get("evaluated", {}).get("evaluated", {}).get("vertices", 0)) > 0
              and abs(attached.get("evaluated", {}).get("bounds", {}).get("min", [999])[0] - 0.25) < 1e-6,
              {"evaluated": attached.get("evaluated")})
        _, key_payload = await _expect_ok(
            session, "live_create_shape_key", "blender_create_shape_key",
            {"object": object_a, "name": shape_key})
        check("shape_key_created",
              key_payload.get("object") == object_a and key_payload.get("created") == shape_key,
              {"object": key_payload.get("object"), "created": key_payload.get("created"),
               "basis_created": key_payload.get("basis_created")})
        await _expect_ok(session, "live_set_driver_input_property", "blender_set_custom_property",
                         {"owner_kind": "object", "owner_name": object_a, "key": "phase", "value": 1.25})
        await _expect_ok(session, "live_set_driven_property", "blender_set_custom_property",
                         {"owner_kind": "object", "owner_name": object_a, "key": "driven_value", "value": 0.0})
        _, driver_payload = await _expect_ok(
            session, "live_add_driver", "blender_add_driver",
            {"owner_kind": "object", "owner_name": object_a, "data_path": '["driven_value"]',
             "expression": "phase",
             "variables": [{"name": "phase", "type": "SINGLE_PROP",
                            "targets": [{"id_type": "OBJECT", "id_name": object_a,
                                         "data_path": '["phase"]'}]}]})
        check("driver_added_and_evaluated",
              driver_payload.get("owner") == "object:%s" % object_a
              and "evaluated_value" in driver_payload,
              {"owner": driver_payload.get("owner"), "evaluated_value": driver_payload.get("evaluated_value")})

        # ---------------------------------------------------------- 5. export scene A
        _, saved = await _expect_ok(
            session, "live_save_scene", "blender_save_scene",
            {"scene": case_a, "relative_path": relative_blend}, timeout=SAVE_TIMEOUT)
        manifest = saved.get("manifest", {})
        check("save_scene_wrote_a_real_file",
              saved.get("saved") is True and int(saved.get("bytes", 0)) > addon.MIN_STAGED_BLEND_BYTES
              and saved.get("container") in ("blend", "gzip", "zstd"),
              {"bytes": saved.get("bytes"), "container": saved.get("container"),
               "publish_mode": saved.get("publish_mode")})
        check("save_scene_contains_exactly_the_case_scene",
              manifest.get("scenes") == [case_a],
              {"scenes": manifest.get("scenes")})
        check("save_scene_contains_exactly_the_intended_objects",
              sorted(manifest.get("objects", [])) == [object_a],
              {"objects": manifest.get("objects")})
        check("save_scene_manifest_has_no_unexpected_data",
              manifest.get("unexpected") == [] and manifest.get("missing") == [],
              {"unexpected": manifest.get("unexpected"), "missing": manifest.get("missing"),
               "ids_written": manifest.get("ids_written"), "ids_proven": manifest.get("ids_proven"),
               "embedded": manifest.get("embedded"), "unmapped": manifest.get("unmapped"),
               "closure_size": manifest.get("closure_size")})
        check("save_scene_leaves_the_main_file_alone",
              saved.get("live_state", {}).get("unchanged") is True
              and saved.get("blend_filepath") == filepath
              and saved.get("live_state", {}).get("current_scene") == case_a,
              {"blend_filepath": saved.get("blend_filepath"),
               "live_state": saved.get("live_state")})
        context["saved"] = {"bytes": saved.get("bytes"), "publish_mode": saved.get("publish_mode"),
                            "manifest": manifest}

        # -------------------------------------------------- 6. read-only inspection
        _, inspected = await _expect_ok(
            session, "live_inspect_blend_file", "blender_inspect_blend_file",
            {"relative_path": relative_blend})
        check("inspect_reports_exactly_the_case_scene",
              inspected.get("scenes") == [case_a]
              and sorted(inspected.get("objects", [])) == [object_a],
              {"scenes": inspected.get("scenes"), "objects": inspected.get("objects")})
        check("inspect_is_read_only_and_library_free",
              inspected.get("opened_into_live_data") is False
              and inspected.get("libraries") == []
              and inspected.get("live_state", {}).get("unchanged") is True,
              {"libraries": inspected.get("libraries"),
               "opened_into_live_data": inspected.get("opened_into_live_data"),
               "live_state": inspected.get("live_state")})
        # Key IDs are owned by their mesh and not exposed as linkable library
        # metadata in Blender 5.2; do not invent a shape_keys inventory.
        check("inspect_exposes_linkable_dependency_collections",
              {"scenes", "objects", "meshes", "node_groups"}.issubset(
                  set(inspected.get("id_types", []))),
              {"id_types": inspected.get("id_types")})

        # -------------------------------- 7. originals unchanged after write+inspect
        _, after_scenes = await _expect_ok(
            session, "live_list_scenes_after_save", "blender_list_scenes", {"include_objects": True})
        after_inventory = _inventory(after_scenes)
        originals_after = {name: objects for name, objects in after_inventory.items()
                           if name in baseline_inventory}
        check("save_and_inspect_left_original_scenes_untouched",
              originals_after == baseline_inventory,
              {"before": baseline_inventory, "after": originals_after})
        check("save_and_inspect_added_only_the_case_scene",
              set(after_inventory) == set(baseline_inventory) | {case_a},
              {"scenes": sorted(after_inventory)})
        _, health_after_save = await _expect_ok(
            session, "live_health_after_save", "blender_health", {})
        check("health_after_save_still_points_at_the_case_scene",
              health_after_save.get("scene") == case_a
              and health_after_save.get("blend_filepath") == filepath,
              {"scene": health_after_save.get("scene"),
               "blend_filepath": health_after_save.get("blend_filepath")})

        # ------------------------------------------------------ 8. rejection matrix
        await _expect_error(session, "reject_overwrite_without_flag", "blender_save_scene",
                            {"scene": case_a, "relative_path": relative_blend, "overwrite": False},
                            needle="file_exists", timeout=SAVE_TIMEOUT)
        _, re_inspected = await _expect_ok(
            session, "inspect_after_overwrite_refusal", "blender_inspect_blend_file",
            {"relative_path": relative_blend})
        check("refused_overwrite_kept_the_published_file",
              re_inspected.get("bytes") == saved.get("bytes")
              and re_inspected.get("scenes") == [case_a],
              {"bytes_before": saved.get("bytes"), "bytes_after": re_inspected.get("bytes")})

        for label, bad_path in (
            ("reject_path_traversal", "../scene_escape.blend"),
            ("reject_path_traversal_nested", "%s/../escape.blend" % ("算法复现",)),
            ("reject_absolute_path", str(WORKSPACE / "Project" / "scene_escape.blend")),
            ("reject_non_blend_suffix", "%s/notes.txt" % relative_dir),
        ):
            await _expect_error(session, label, "blender_save_scene",
                                {"scene": case_a, "relative_path": bad_path}, needle="invalid_path")
        check("escaped_path_wrote_nothing", not (WORKSPACE / "scene_escape.blend").exists(),
              {"probe": str(WORKSPACE / "scene_escape.blend")})

        await _expect_error(session, "reject_missing_scene_save", "blender_save_scene",
                            {"scene": "%s_missing" % prefix,
                             "relative_path": "%s/missing_scene.blend" % relative_dir},
                            needle="scene_not_found")
        await _expect_error(session, "reject_missing_scene_switch", "blender_switch_scene",
                            {"name": "%s_missing" % prefix}, needle="scene_not_found")
        await _expect_error(session, "reject_inspect_missing_file", "blender_inspect_blend_file",
                            {"relative_path": "%s/does_not_exist.blend" % relative_dir},
                            needle="file_not_found")

        # ------------------------------------------- 9. collision + next empty scene
        await _expect_error(session, "reject_duplicate_case_scene_name", "blender_create_case_scene",
                            {"name": case_a, "activate": True}, needle="already_exists")
        _, collision_scenes = await _expect_ok(
            session, "list_scenes_after_collision", "blender_list_scenes", {"include_objects": True})
        collision_inventory = _inventory(collision_scenes)
        check("duplicate_name_was_not_auto_suffixed",
              "%s.001" % case_a not in collision_inventory
              and collision_inventory.get(case_a) == [object_a],
              {"scenes": sorted(collision_inventory)})

        _, created_b = await _expect_ok(
            session, "live_create_case_scene_B", "blender_create_case_scene",
            {"name": case_b, "activate": True})
        check("next_case_scene_is_empty_and_does_not_disturb_A",
              created_b.get("empty", {}).get("empty") is True
              and created_b.get("previous_scene") == case_a,
              {"empty": created_b.get("empty"), "previous": created_b.get("previous_scene")})
        _, object_b_payload = await _expect_ok(
            session, "live_create_object_in_B", "blender_create_mesh_object",
            {"name": object_b,
             "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
             "faces": [[0, 1, 2]]})
        check("object_created_in_B", object_b_payload.get("created") == object_b, object_b_payload)
        await _expect_ok(session, "live_switch_back_to_A", "blender_switch_scene", {"name": case_a})

        # ------------------------- 10. external dependency must be refused (no write)
        await _expect_ok(session, "live_set_external_driven_property", "blender_set_custom_property",
                         {"owner_kind": "object", "owner_name": object_a, "key": "driven_ext", "value": 0.0})
        _, external_driver = await _expect_ok(
            session, "live_add_external_driver", "blender_add_driver",
            {"owner_kind": "object", "owner_name": object_a, "data_path": '["driven_ext"]',
             "expression": "ext",
             "variables": [{"name": "ext", "type": "TRANSFORMS",
                            "targets": [{"id_type": "OBJECT", "id_name": object_b,
                                         "transform_type": "LOC_Z"}]}]})
        check("external_driver_targets_the_other_scene",
              external_driver.get("owner") == "object:%s" % object_a,
              {"owner": external_driver.get("owner"),
               "variables": external_driver.get("variables")})
        await _expect_error(session, "reject_external_object_dependency", "blender_save_scene",
                            {"scene": case_a, "relative_path": relative_external},
                            needle="external_dependency", timeout=SAVE_TIMEOUT)
        await _expect_error(session, "reject_external_dependency_wrote_nothing",
                            "blender_inspect_blend_file", {"relative_path": relative_external},
                            needle="file_not_found")

        # ------------------------------------------------------- 11. restore + verify
        _, final_scenes = await _expect_ok(
            session, "live_list_scenes_final", "blender_list_scenes", {"include_objects": True})
        final_inventory = _inventory(final_scenes)
        check("case_scenes_hold_exactly_their_own_objects",
              final_inventory.get(case_a) == [object_a] and final_inventory.get(case_b) == [object_b],
              {"A": final_inventory.get(case_a), "B": final_inventory.get(case_b)})
        check("original_scenes_are_bitwise_unchanged",
              {name: objects for name, objects in final_inventory.items()
               if name in baseline_inventory} == baseline_inventory,
              {"before": baseline_inventory,
               "after": {name: obj for name, obj in final_inventory.items() if name in baseline_inventory}})
        check("nothing_was_deleted",
              set(final_inventory) == set(baseline_inventory) | {case_a, case_b},
              {"scenes": sorted(final_inventory)})

        _, restored = await _expect_ok(session, "live_restore_original_scene", "blender_switch_scene",
                                       {"name": original_scene})
        _, health_final = await _expect_ok(session, "live_health_final", "blender_health", {})
        check("original_scene_is_active_again_and_file_path_unchanged",
              health_final.get("scene") == original_scene
              and health_final.get("blend_filepath") == filepath
              and restored.get("current_scene") == original_scene,
              {"scene": health_final.get("scene"), "blend_filepath": health_final.get("blend_filepath"),
               "restored": restored.get("current_scene")})
        context["finished_at"] = time.time()
        context["duration_s"] = round(context["finished_at"] - context["started_at"], 3)
        return context


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def _run_unit_tests() -> tuple[bool, dict]:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    started = time.time()
    result = runner.run(suite)
    return result.wasSuccessful(), {
        "tests_run": result.testsRun,
        "failures": [str(test) for test, _ in result.failures],
        "errors": [str(test) for test, _ in result.errors],
        "skipped": [str(test) for test, _ in result.skipped],
        "duration_s": round(time.time() - started, 3),
    }


def _write_report(payload: dict) -> tuple[Path, Path]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    detailed = RUNTIME / "logs" / ("scene_isolation_%s.json" % stamp)
    latest = RUNTIME / "scene_isolation_report.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    detailed.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return latest, detailed


def main() -> int:
    live = False
    if "--live" in sys.argv:
        live = True
        sys.argv.remove("--live")
    if os.environ.get("BLENDER_MCP_SCENE_ISOLATION_LIVE") == "1":
        live = True

    unit_ok, unit_summary = _run_unit_tests()
    live_ok = True
    live_context: dict = {}
    live_error: str | None = None
    if live:
        try:
            async def bounded_live_run():
                return await asyncio.wait_for(run_live_acceptance(), timeout=180)
            live_context = asyncio.run(bounded_live_run())
        except (LiveUnavailable, AcceptanceAborted) as exc:
            live_ok = False
            live_error = str(exc)
            record("live_run_completed", False, {"message": live_error})
        except Exception as exc:  # noqa: BLE001
            live_ok = False
            live_error = "%s: %s" % (type(exc).__name__, exc)
            record("live_acceptance_crashed", False, {"traceback": traceback.format_exc()[-1500:]})
        if LIVE_RESULTS:
            live_ok = live_ok and all(entry["ok"] for entry in LIVE_RESULTS)

    payload = {
        "suite": "scene_isolation",
        "live_requested": live,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "blender_addon_imported_with_real_bpy": _HAVE_REAL_BPY,
        "unit": unit_summary,
        "unit_ok": unit_ok,
        "live_ok": live_ok if live else None,
        "live_error": live_error,
        "live_run": {
            "checks_total": len(LIVE_RESULTS),
            "checks_failed": [entry["name"] for entry in LIVE_RESULTS if not entry["ok"]],
            "context": live_context,
        },
        "live_checks": LIVE_RESULTS,
        "ok": unit_ok and (live_ok if live else True),
        "warnings": [
            "The live acceptance run leaves the case scenes and the exported .blend in place: nothing is "
            "deleted by design (the MCP surface has no delete tool).",
            "Scene activation is only attempted when Blender exposes a window; without one "
            "create_case_scene(activate=true) fails with context_unavailable instead of faking a switch.",
        ],
    }
    latest, detailed = _write_report(payload)
    summary = {
        "suite": payload["suite"],
        "live": live,
        "unit_ok": unit_ok,
        "unit_tests": unit_summary["tests_run"],
        "unit_failures": unit_summary["failures"] + unit_summary["errors"],
        "live_ok": payload["live_ok"],
        "live_checks": len(LIVE_RESULTS),
        "live_failed": payload["live_run"]["checks_failed"],
        "live_error": live_error,
        "report": str(latest),
        "detailed_log": str(detailed),
        "ok": payload["ok"],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
