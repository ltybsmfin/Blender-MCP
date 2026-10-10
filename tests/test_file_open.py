"""Unit proof for the named "open an existing project .blend" capability.

Scope: ``blender_mcp/tools/file_open.py`` (typed MCP tool ``blender_open_blend``)
and ``blender/file_open.py`` (bridge command ``open_blend``).

Everything here runs **without Blender and without a socket**: ``bpy`` and the
add-on ``api`` are stubbed, and the bridge is a scripted fake.  No .blend file is
created, written or modified, no Blender process is started or stopped, and no
raw socket is opened.  A real 5.2.1 acceptance run is a separate, live step (it
is *not* claimed here).

Run::

    .venv/Scripts/python.exe tests/test_file_open.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender_mcp import audit, config  # noqa: E402
from blender_mcp.bridge import BlenderCommandError, BridgeUnavailable  # noqa: E402
from blender_mcp.tools import file_open as mcp_file_open  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

PROJECT = config.project_dir()
EXISTING_BLEND = next((path for path in sorted(PROJECT.rglob("*.blend")) if path.is_file()), None)
EXISTING_DIR = next((path for path in sorted(PROJECT.iterdir()) if path.is_dir()), None)
#: A volume different from the project's, for the cross-drive containment check.
_PROJECT_DRIVE = os.path.splitdrive(str(PROJECT))[0].upper()
_OTHER_DRIVE = "D:" if _PROJECT_DRIVE != "D:" else "C:"
_IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# shared fakes
# --------------------------------------------------------------------------- #
class FakeBridge:
    """Scripted ``BridgeClient`` stand-in.  Records every command, never sends one."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self.closed = 0
        self.address = "fake:0"
        self.calls_made = 0

    def call(self, command, params=None, timeout=None):
        params = dict(params or {})
        seen = sum(1 for entry in self.calls if entry["command"] == command) + 1
        self.calls.append({"command": command, "params": params, "timeout": timeout})
        self.calls_made += 1
        return self.handler(command, params, seen)

    def close(self):
        self.closed += 1

    def commands(self):
        return [entry["command"] for entry in self.calls]


class CapturingMCP:
    """Records the functions registered by ``register(mcp, bridge)``."""

    def __init__(self):
        self.tools = {}

    def tool(self, name=None, description=None, **kwargs):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_bridge(open_reply=None, health=None, commands=("open_blend",), open_error=None,
                 health_failures=0, command_failures=0, health_pid=None, health_path=None):
    """A fake bridge scripted per command; only reads may fail transiently."""
    state = {"health_failures": health_failures, "command_failures": command_failures}
    health_payload = dict(health) if health is not None else {}
    if health_pid is not None:
        health_payload["pid"] = health_pid
    if health_path is not None:
        health_payload["blend_filepath"] = health_path
    health_payload.setdefault("bridge", "blender-mcp")
    health_payload.setdefault("protocol", 1)
    health_payload.setdefault("blender_version", "5.2.1")
    health_payload.setdefault("scene", "Case")
    health_payload.setdefault("background", False)

    def handler(command, params, attempt):
        if command == "open_blend":
            if open_error is not None:
                raise open_error
            return dict(open_reply or {})
        if command == "health":
            if state["health_failures"] > 0:
                state["health_failures"] -= 1
                raise BridgeUnavailable("health transport down (injected)")
            return dict(health_payload)
        if command == "list_commands":
            if state["command_failures"] > 0:
                state["command_failures"] -= 1
                raise BridgeUnavailable("list_commands transport down (injected)")
            return {"commands": [{"name": name, "mutating": True, "summary": ""} for name in commands]}
        raise AssertionError("unexpected bridge command %r" % command)

    return FakeBridge(handler)


def tool_function(bridge):
    capture = CapturingMCP()
    mcp_file_open.register(capture, bridge)
    return capture.tools["blender_open_blend"]


# --------------------------------------------------------------------------- #
# Blender-side fakes
# --------------------------------------------------------------------------- #
class SideError(Exception):
    """Stand-in for the add-on's ``CommandError``."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class FakeApi:
    """Stand-in for the add-on module passed to ``register_commands(api)``."""

    def __init__(self, project_root, container=None, metadata=None,
                 container_error=None, metadata_error=None, fingerprint=None, on_metadata=None):
        self.CommandError = SideError
        self.commands = {}
        self.command_meta = {}
        self.project_root = Path(project_root)
        self.container_calls = []
        self.metadata_calls = []
        self.resolved = []
        self.container = container if container is not None else {
            "bytes": 1234, "container": "blend", "magic": "424c454e"}
        self.metadata = metadata if metadata is not None else {
            "scenes": ["Case"], "objects": ["Cube"], "libraries": []}
        self.container_error = container_error
        self.metadata_error = metadata_error
        #: Optional stand-in for the add-on's full read-only live-state fingerprint.
        self._live_state_fingerprint = fingerprint
        self.on_metadata = on_metadata

    def command(self, name, mutating=True, summary=""):
        def decorator(func):
            self.commands[name] = func
            self.command_meta[name] = {"mutating": mutating, "summary": summary}
            return func

        return decorator

    def project_dir(self):
        return self.project_root

    def resolve_project_path(self, relative_path):
        """Mirrors the add-on policy: relative only, no '..', must stay inside."""
        raw = str(relative_path).replace("/", os.sep)
        if not raw or not raw.strip():
            raise SideError("invalid_path", "path must not be empty")
        if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
            raise SideError("invalid_path", "path must be relative to <workspace>/Project")
        parts = [part for part in raw.split(os.sep) if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise SideError("invalid_path", "path traversal ('..') is rejected")
        if not parts:
            raise SideError("invalid_path", "path does not name a file")
        self.resolved.append("/".join(parts))
        candidate = self.project_root.joinpath(*parts)
        real = Path(os.path.realpath(str(candidate)))
        if os.path.normcase(str(real)) != os.path.normcase(str(candidate)) \
                and not str(real).startswith(os.path.normcase(str(self.project_root))):
            raise SideError("path_outside_project", "resolved path escapes <workspace>/Project")
        return real

    def _blend_container(self, path):
        self.container_calls.append(str(path))
        if self.container_error is not None:
            raise self.container_error
        return self.container

    def _temp_blend_metadata(self, path):
        self.metadata_calls.append(str(path))
        if self.metadata_error is not None:
            raise self.metadata_error
        if self.on_metadata is not None:
            self.on_metadata()
        return self.metadata


class FakeScene:
    def __init__(self, name):
        self.name = name


class FakeOperator:
    """``bpy.ops.wm.open_mainfile`` stand-in with introspectable RNA."""

    def __init__(self, properties=None, result=("FINISHED",), on_call=None, raise_error=None):
        self.properties = properties if properties is not None else {
            "filepath": "STRING", "load_ui": "BOOLEAN", "use_scripts": "BOOLEAN"}
        self.result = result
        self.on_call = on_call
        self.raise_error = raise_error
        self.calls = []

    def get_rna_type(self):
        if self.properties is None:
            raise RuntimeError("injected: RNA introspection unavailable")
        return types.SimpleNamespace(
            properties=[types.SimpleNamespace(identifier=name, type=kind)
                        for name, kind in self.properties.items()])

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.raise_error is not None:
            raise self.raise_error
        if self.on_call is not None:
            self.on_call(kwargs)
        return set(self.result)


class FakeBpy:
    def __init__(self, filepath="", dirty=False, scene="Before", operator=None):
        self.data = types.SimpleNamespace(filepath=filepath, is_dirty=dirty, objects=[],
                                         scenes=[FakeScene(scene)], window_managers=[])
        self.context = types.SimpleNamespace(scene=FakeScene(scene))
        self.app = types.SimpleNamespace(background=False, version_string="5.2.1")
        self.ops = types.SimpleNamespace(wm=types.SimpleNamespace(open_mainfile=operator or FakeOperator()))


def load_bridge_module(bpy_stub):
    """Import ``blender/file_open.py`` with a stub ``bpy`` (no Blender needed)."""
    previous = sys.modules.get("bpy")
    sys.modules["bpy"] = bpy_stub
    try:
        path = ROOT / "blender" / "file_open.py"
        spec = importlib.util.spec_from_file_location("blender_side_file_open", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = previous
    return module


# --------------------------------------------------------------------------- #
# MCP surface: schema, defaults, strict booleans, untouched neighbours
# --------------------------------------------------------------------------- #
class SchemaTests(unittest.TestCase):
    EXISTING_MODULES = ("meta", "scene", "geometry_nodes", "shape_keys", "drivers",
                        "launcher", "preview", "operation_log", "node_frames", "node_notes")

    def _register_existing(self, server):
        from blender_mcp.tools import (drivers, geometry_nodes, launcher, meta, node_frames,
                                       node_notes, operation_log, preview, scene, shape_keys)
        modules = {"meta": meta, "scene": scene, "geometry_nodes": geometry_nodes,
                   "shape_keys": shape_keys, "drivers": drivers, "launcher": launcher,
                   "preview": preview, "operation_log": operation_log,
                   "node_frames": node_frames, "node_notes": node_notes}

        class Stub:
            def call(self, *args, **kwargs):
                return {}

        for name in self.EXISTING_MODULES:
            modules[name].register(server, Stub())
        return server

    def test_new_tool_schema_and_defaults(self):
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("open-schema")
        mcp_file_open.register(server, build_bridge(open_reply={}))
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(list(tools), ["blender_open_blend"])
        schema = tools["blender_open_blend"].inputSchema
        self.assertEqual(schema["required"], ["path"])
        self.assertEqual(schema["properties"]["path"]["type"], "string")
        for name in ("confirm_replace_current", "load_ui"):
            self.assertEqual(schema["properties"][name]["type"], "boolean")
            self.assertIs(schema["properties"][name]["default"], False)

    def test_strict_booleans_reject_strings_and_numbers(self):
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("open-strict")
        called = []

        def handler(command, params, attempt):
            called.append(command)
            return {}

        mcp_file_open.register(server, FakeBridge(handler))
        for value in ("true", "false", 1, 0, "1", None):
            with self.assertRaises(Exception) as caught:
                asyncio.run(server.call_tool("blender_open_blend",
                                             {"path": "x.blend", "confirm_replace_current": value}))
            self.assertIn("valid boolean", str(caught.exception))
        self.assertEqual(called, [])

    def test_existing_sixty_tools_keep_their_schemas(self):
        from blender_mcp.tools import AuditedFastMCP

        baseline_server = self._register_existing(AuditedFastMCP("baseline"))
        baseline = {tool.name: tool.model_dump(exclude={"description"})
                    for tool in asyncio.run(baseline_server.list_tools())}
        self.assertEqual(len(baseline), 60)

        extended_server = self._register_existing(AuditedFastMCP("extended"))
        mcp_file_open.register(extended_server, build_bridge(open_reply={}))
        extended = {tool.name: tool.model_dump(exclude={"description"})
                    for tool in asyncio.run(extended_server.list_tools())}
        self.assertEqual(len(extended), 61)
        self.assertIn("blender_open_blend", extended)
        for name, schema in baseline.items():
            self.assertEqual(extended[name], schema, name)

    def test_module_does_not_import_bpy_and_does_not_patch_shared_helpers(self):
        resolver = config.resolve_project_path
        projector = config.project_dir
        mcp_file_open.register(CapturingMCP(), build_bridge(open_reply={}))
        self.assertNotIn("bpy", sys.modules)
        self.assertIs(config.resolve_project_path, resolver)
        self.assertIs(config.project_dir, projector)

    def test_read_only_inspect_tool_behaviour_unchanged(self):
        from blender_mcp.tools import scene as scene_module

        calls = []

        def handler(command, params, attempt):
            calls.append((command, params))
            return {"inspected": True, "commands": command}

        capture = CapturingMCP()
        scene_module.register(capture, FakeBridge(handler))
        tool = capture.tools["blender_inspect_blend_file"]
        with self.assertRaises(ToolError) as caught:
            tool(relative_path=os.path.join("..", "outside.blend"))
        self.assertTrue(str(caught.exception).startswith("invalid_path:"))
        self.assertEqual(calls, [], "the read-only inspector still refuses before the bridge")
        if EXISTING_BLEND is None:
            self.skipTest("no project .blend available for the inspect regression")
        relative = os.path.relpath(str(EXISTING_BLEND), str(PROJECT)).replace(os.sep, "/")
        self.assertEqual(tool(relative_path=relative), {"inspected": True, "commands": "inspect_blend_file"})
        self.assertEqual(calls, [("inspect_blend_file", {"relative_path": relative})])


# --------------------------------------------------------------------------- #
# MCP surface: path policy (layer 1) - a rejected call never reaches the bridge
# --------------------------------------------------------------------------- #
class McpPathTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(mcp_file_open, "VERIFY_BUDGET_SECONDS", 0.3),
            mock.patch.object(mcp_file_open, "VERIFY_POLL_INTERVAL", 0.01),
            mock.patch.object(mcp_file_open, "VERIFY_CALL_TIMEOUT", 0.2),
            mock.patch.object(mcp_file_open, "OPEN_CALL_TIMEOUT", 1.0),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.bridge = build_bridge(open_reply={
            "opened": True, "path": str(EXISTING_BLEND) if EXISTING_BLEND else "", "pid": 4242})
        self.tool = tool_function(self.bridge)

    def expect(self, code, path, **kwargs):
        with self.assertRaises(ToolError) as caught:
            self.tool(path=path, **kwargs)
        message = str(caught.exception)
        self.assertTrue(message.startswith(code + ":"), message)
        self.assertEqual(self.bridge.calls, [], "a refused path must not reach Blender")
        return message

    def test_empty_relative_and_type_errors(self):
        self.expect("invalid_path", "   ")
        self.expect("invalid_argument", 42)
        self.expect("invalid_path", "case.blend")
        self.expect("invalid_path", "demo" + os.sep + "case.blend")

    def test_unc_device_and_nul(self):
        self.expect("unc_path", r"\\server\share\case.blend")
        self.expect("unc_path", "//server/share/case.blend")
        self.expect("device_path", r"\\?\D:\AI\MCP\Blender\Project\case.blend")
        self.expect("device_path", r"\\.\PhysicalDrive0")
        self.expect("invalid_path", str(PROJECT / "case\x00.blend"))

    def test_unnormalized_and_escaping_paths(self):
        self.expect("invalid_path", str(PROJECT / "demo" / ".." / "demo" / "a.blend"))
        self.expect("invalid_path", str(PROJECT / "demo") + os.sep)
        self.expect("path_outside_project", str(config.WORKSPACE_ROOT / "outside.blend"))
        self.expect("path_outside_project", str(PROJECT.parent / "sibling.blend"))

    @unittest.skipUnless(_IS_WINDOWS, "drive/rooted path forms are Windows-specific")
    def test_cross_volume_rooted_and_drive_relative_paths(self):
        # Different volume: os.path.relpath would raise ValueError - it must be a
        # structured containment failure, not an internal crash.
        cross = _OTHER_DRIVE + os.sep + "AI" + os.sep + "MCP" + os.sep + "Blender" + os.sep + \
            "Project" + os.sep + "demo" + os.sep + "case.blend"
        message = self.expect("path_outside_project", cross)
        self.assertIn("different volume", message)
        # Rooted without a drive: os.path.isabs() says True, but it is not usable.
        self.expect("invalid_path", os.sep + "AI" + os.sep + "MCP" + os.sep + "Blender" + os.sep +
                    "Project" + os.sep + "case.blend")
        # Drive-relative (per-drive cwd) must never be accepted.
        self.expect("invalid_path", "C:foo" + os.sep + "case.blend")
        self.expect("invalid_path", _PROJECT_DRIVE + "relative" + os.sep + "case.blend")

    def test_absolute_syntax_helper_covers_both_platforms(self):
        ok = mcp_file_open._absolute_syntax_ok
        self.assertTrue(ok("D:\\AI\\MCP\\Blender\\Project\\case.blend", windows=True))
        self.assertFalse(ok("\\AI\\MCP\\Blender\\Project\\case.blend", windows=True))
        self.assertFalse(ok("C:foo\\case.blend", windows=True))
        self.assertFalse(ok("\\\\server\\share\\case.blend", windows=True))
        self.assertFalse(ok("relative\\case.blend", windows=True))
        self.assertTrue(ok("/srv/project/case.blend", windows=False))
        self.assertFalse(ok("C:\\AI\\Project\\case.blend", windows=False))
        self.assertFalse(ok("relative/case.blend", windows=False))
        # The platform-native call (no explicit flag) must agree with the running OS.
        native = "D:\\AI\\MCP\\Blender\\Project\\case.blend" if _IS_WINDOWS else "/srv/project/case.blend"
        self.assertTrue(ok(native))

    def test_extension_missing_and_directory(self):
        self.expect("invalid_path", str(PROJECT / "demo" / "note.txt"))
        self.expect("file_not_found", str(PROJECT / "demo" / "definitely_missing_9f3a.blend"))
        if EXISTING_DIR is not None:
            self.expect("not_a_file", str(EXISTING_DIR))

    def test_unreadable_target(self):
        with mock.patch.object(mcp_file_open, "_file_kind", return_value="file"), \
                mock.patch.object(mcp_file_open, "_probe_readable", return_value=False):
            self.expect("unreadable", str(PROJECT / "demo" / "unreadable_case.blend"))

    def test_reparse_point_components_are_delegated_and_refused(self):
        project_key = os.path.normcase(str(PROJECT))

        def fake_reparse(path):
            # Only the components below the root pretend to be reparse points, so
            # project_dir() itself still accepts the canonical root.
            return os.path.normcase(str(path)) != project_key

        with mock.patch.object(config, "_is_reparse_point", side_effect=fake_reparse):
            if EXISTING_DIR is not None:
                message = self.expect("invalid_path", str(EXISTING_DIR / "case.blend"))
                self.assertIn("symlink", message)
            if EXISTING_BLEND is not None:
                message = self.expect("invalid_path", str(EXISTING_BLEND))
                self.assertIn("symlink", message)

    @unittest.skipUnless(EXISTING_BLEND is not None, "no project .blend available")
    def test_valid_path_is_normalized_and_forwarded_unchanged(self):
        reply = {"opened": True, "path": str(EXISTING_BLEND), "pid": 4242}
        bridge = build_bridge(open_reply=reply, health_path=str(EXISTING_BLEND), health_pid=4242)
        tool = tool_function(bridge)
        result = tool(path=str(EXISTING_BLEND).replace(os.sep, "/"), confirm_replace_current=True)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(bridge.commands(), ["open_blend", "health", "list_commands"])
        params = bridge.calls[0]["params"]
        self.assertEqual(params["path"], str(EXISTING_BLEND))
        self.assertIs(params["confirm_replace_current"], True)
        self.assertIs(params["load_ui"], False)
        self.assertEqual(bridge.calls[0]["timeout"], 1.0)

    @unittest.skipUnless(EXISTING_BLEND is not None, "no project .blend available")
    def test_confirmation_flag_is_forwarded_not_decided_locally(self):
        reply = {"opened": True, "path": str(EXISTING_BLEND), "pid": 7}
        bridge = build_bridge(open_reply=reply, health_path=str(EXISTING_BLEND), health_pid=7)
        tool = tool_function(bridge)
        tool(path=str(EXISTING_BLEND))
        self.assertIs(bridge.calls[0]["params"]["confirm_replace_current"], False)
        tool(path=str(EXISTING_BLEND), load_ui=True)
        self.assertIs(bridge.calls[3]["params"]["load_ui"], True)


# --------------------------------------------------------------------------- #
# MCP surface: bounded read-only verification and no replay
# --------------------------------------------------------------------------- #
class McpVerificationTests(unittest.TestCase):
    PATH = "C" + os.sep + "Project" + os.sep + "case.blend"

    def setUp(self):
        self._patches = [
            mock.patch.object(mcp_file_open, "VERIFY_BUDGET_SECONDS", 0.3),
            mock.patch.object(mcp_file_open, "VERIFY_POLL_INTERVAL", 0.01),
            mock.patch.object(mcp_file_open, "VERIFY_CALL_TIMEOUT", 0.2),
            mock.patch.object(mcp_file_open, "OPEN_CALL_TIMEOUT", 1.0),
            mock.patch.object(mcp_file_open, "_resolve_target", lambda path: Path(self.PATH)),
            mock.patch.object(mcp_file_open, "_file_kind", lambda path: "file"),
            mock.patch.object(mcp_file_open, "_probe_readable", lambda path: True),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def reply(self, path=None, pid=4242):
        return {"opened": True, "path": path or self.PATH, "pid": pid, "current": {"scene": "Case"}}

    def test_success_is_verified_read_only(self):
        bridge = build_bridge(open_reply=self.reply(), health_path=self.PATH, health_pid=4242)
        result = tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(result["opened"])
        verification = result["verification"]
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["mode"], "reply")
        self.assertTrue(verification["path_matches"])
        self.assertTrue(verification["pid_matches"])
        self.assertTrue(verification["open_blend_listed"])
        self.assertFalse(verification["open_blend_replayed"])
        self.assertEqual(verification["commands_retried"], ["health", "list_commands"])
        self.assertEqual(bridge.commands(), ["open_blend", "health", "list_commands"])
        self.assertEqual(bridge.closed, 0)

    def test_health_drops_once_then_recovers_without_replaying_open(self):
        bridge = build_bridge(open_reply=self.reply(), health_path=self.PATH, health_pid=4242,
                              health_failures=1)
        result = tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["verification"]["attempts"], 2)
        self.assertEqual(bridge.commands().count("open_blend"), 1)
        self.assertGreaterEqual(bridge.closed, 1)

    def test_reads_that_keep_failing_never_report_success(self):
        bridge = build_bridge(open_reply=self.reply(), health_path=self.PATH, health_pid=4242,
                              health_failures=10_000)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertEqual(bridge.commands().count("open_blend"), 1)
        self.assertGreater(bridge.commands().count("health"), 1)

    def test_command_roster_must_list_open_blend(self):
        bridge = build_bridge(open_reply=self.reply(), health_path=self.PATH, health_pid=4242,
                              commands=("health", "ping"))
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertIn("open_blend_listed", str(caught.exception))
        self.assertEqual(bridge.commands().count("open_blend"), 1)

    def test_wrong_pid_is_a_verification_failure(self):
        bridge = build_bridge(open_reply=self.reply(pid=4242), health_path=self.PATH, health_pid=999)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertIn("pid_matches", str(caught.exception))

    def test_wrong_filepath_is_a_verification_failure(self):
        bridge = build_bridge(open_reply=self.reply(),
                              health_path=self.PATH + ".other", health_pid=4242)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertIn("path_matches", str(caught.exception))

    def test_reply_naming_another_file_is_refused_before_verification(self):
        bridge = build_bridge(open_reply=self.reply(path=self.PATH + ".other"),
                              health_path=self.PATH, health_pid=4242)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertIn("reply_path_mismatch", str(caught.exception))
        self.assertEqual(bridge.commands(), ["open_blend"])

    def test_missing_positive_payload_is_not_a_success(self):
        bridge = build_bridge(open_reply={"path": self.PATH})
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("load_failed:"))
        self.assertEqual(bridge.commands(), ["open_blend"])

    def test_remote_command_error_keeps_code_stage_and_details(self):
        for remote in ("unsaved_changes", "confirmation_required", "load_failed",
                       "invalid_blend", "unsupported_api"):
            bridge = build_bridge(open_error=BlenderCommandError(
                "open_blend", remote, "refused in Blender", {"path": self.PATH}))
            with self.assertRaises(ToolError) as caught:
                tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
            message = str(caught.exception)
            self.assertTrue(message.startswith(remote + ":"), message)
            self.assertIn("stage:", message)
            self.assertIn("details:", message)
            self.assertEqual(bridge.commands(), ["open_blend"])

    def test_transport_failure_without_evidence_is_not_a_success(self):
        bridge = build_bridge(open_error=BridgeUnavailable("connection lost (injected)"),
                              health_path=self.PATH, health_pid=4242)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        message = str(caught.exception)
        self.assertTrue(message.startswith("load_pending:"), message)
        self.assertEqual(bridge.commands().count("open_blend"), 1)
        self.assertIn("health", bridge.commands())

    def test_transport_failure_with_other_filepath_fails_verification(self):
        bridge = build_bridge(open_error=BridgeUnavailable("connection lost (injected)"),
                              health_path=self.PATH + ".other", health_pid=4242)
        with self.assertRaises(ToolError) as caught:
            tool_function(bridge)(path=self.PATH, confirm_replace_current=True)
        self.assertTrue(str(caught.exception).startswith("verification_failed:"))
        self.assertEqual(bridge.commands().count("open_blend"), 1)

    def _evidence(self, request_id="req-open-1", source="bridge", ok=True, result=None):
        payload = {"opened": True, "path": self.PATH, "pid": 4242, "use_scripts": False, "load_ui": False}
        if result is not None:
            payload = result
        return [{
            "event": "command_end", "command": "open_blend", "request_id": request_id,
            "source": source, "pid": 4242, "session": "abc", "ts": "2026-10-05T00:00:00.000Z",
            "ok": ok, "result": payload,
        }]

    def _post_hoc(self, request_id, evidence, **request_kwargs):
        bridge = build_bridge(open_error=BridgeUnavailable("connection lost (injected)"),
                              health_path=self.PATH, health_pid=4242)
        with mock.patch.object(mcp_file_open.audit, "tail_events", return_value=evidence):
            with audit.request_scope(request_id):
                try:
                    result = tool_function(bridge)(path=self.PATH, **request_kwargs)
                except ToolError as exc:
                    return None, str(exc), bridge
        return result, None, bridge

    def test_transport_failure_with_matching_audit_evidence_is_verified(self):
        result, error, bridge = self._post_hoc(
            "req-open-1", self._evidence("req-open-1"), confirm_replace_current=True)
        self.assertIsNone(error)
        self.assertTrue(result["opened"])
        self.assertTrue(result["verified_after_transport_failure"])
        self.assertEqual(result["verification"]["mode"], "post_hoc")
        self.assertTrue(result["verification"]["request_evidence"]["found"])
        self.assertTrue(result["verification"]["request_evidence"]["confirmed"])
        self.assertEqual(bridge.commands().count("open_blend"), 1)

    def test_post_hoc_success_payload_carries_health_and_requested_options(self):
        result, error, bridge = self._post_hoc(
            "req-open-1", self._evidence("req-open-1"), confirm_replace_current=True)
        self.assertIsNone(error)
        # Values observed through the read-only health command, never guessed.
        self.assertEqual(result["current_filepath"], self.PATH)
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["blender_version"], "5.2.1")
        self.assertEqual(result["current_scene"], "Case")
        # Safety/UI options: the request's load_ui plus the evidence-confirmed script flag.
        self.assertIs(result["load_ui"], False)
        self.assertIs(result["use_scripts"], False)
        self.assertIs(result["ui_mode"]["load_ui"], False)
        self.assertIs(result["ui_mode"]["background"], False)
        self.assertTrue(result["safety_options_confirmed_by_evidence"]["use_scripts_false"])
        self.assertIsNone(result["previous"])
        self.assertIn("not observed", result["previous_unavailable"])
        self.assertFalse(result["target_written"])
        self.assertEqual(result["rollback"], "not_attempted")
        self.assertNotIn("open_blend", bridge.commands()[1:],
                         "only the open is ever sent once; verification is read-only")

    def test_post_hoc_load_ui_true_is_reported_from_the_request(self):
        recorded = {"opened": True, "path": self.PATH, "pid": 4242,
                    "use_scripts": False, "load_ui": True}
        result, error, _bridge = self._post_hoc(
            "req-open-1", self._evidence("req-open-1", result=recorded),
            confirm_replace_current=True, load_ui=True)
        self.assertIsNone(error)
        self.assertIs(result["load_ui"], True)
        self.assertIs(result["ui_mode"]["load_ui"], True)
        self.assertIs(result["use_scripts"], False)

    def test_post_hoc_rejects_evidence_from_another_writer(self):
        for source in ("mcp", "", None):
            with self.subTest(source=source):
                result, error, _bridge = self._post_hoc(
                    "req-open-1", self._evidence("req-open-1", source=source),
                    confirm_replace_current=True)
                self.assertIsNone(result)
                self.assertTrue(error.startswith("load_pending:"), error)

    def test_post_hoc_evidence_without_safety_confirmation_is_pending(self):
        cases = {
            "no use_scripts": {"opened": True, "path": self.PATH, "pid": 4242, "load_ui": False},
            "use_scripts true": {"opened": True, "path": self.PATH, "pid": 4242,
                                 "use_scripts": True, "load_ui": False},
            "not opened": {"path": self.PATH, "pid": 4242, "use_scripts": False, "load_ui": False},
            "other path": {"opened": True, "path": self.PATH + ".other", "pid": 4242,
                           "use_scripts": False, "load_ui": False},
        }
        for label, recorded in cases.items():
            with self.subTest(label=label):
                result, error, _bridge = self._post_hoc(
                    "req-open-1", self._evidence("req-open-1", result=recorded),
                    confirm_replace_current=True)
                self.assertIsNone(result)
                self.assertTrue(error.startswith("load_pending:"), error)

    def test_post_hoc_evidence_load_ui_mismatch_is_pending(self):
        recorded = {"opened": True, "path": self.PATH, "pid": 4242, "use_scripts": False, "load_ui": True}
        result, error, _bridge = self._post_hoc(
            "req-open-1", self._evidence("req-open-1", result=recorded), confirm_replace_current=True)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("load_pending:"), error)

    def test_deadline_is_checked_before_every_read_and_clamps_each_timeout(self):
        clock = {"now": 1000.0}
        stamps = []

        def handler(command, params, attempt):
            stamps.append(clock["now"])
            raise BridgeUnavailable("health transport down (injected)")

        bridge = FakeBridge(handler)
        with mock.patch.object(mcp_file_open, "VERIFY_BUDGET_SECONDS", 10.0), \
                mock.patch.object(mcp_file_open, "VERIFY_CALL_TIMEOUT", 3.0), \
                mock.patch.object(mcp_file_open, "VERIFY_POLL_INTERVAL", 0.25), \
                mock.patch.object(mcp_file_open.time, "monotonic", side_effect=lambda: clock["now"]), \
                mock.patch.object(mcp_file_open.time, "sleep",
                                  side_effect=lambda seconds: clock.__setitem__("now", clock["now"] + seconds)):
            report = mcp_file_open._bounded_verify(bridge, Path(self.PATH), 4242, 0.0, "reply")
        deadline = 1000.0 + 10.0
        self.assertTrue(report["deadline_exhausted"])
        self.assertGreaterEqual(report["attempts"], 10)
        self.assertGreaterEqual(report["reads_skipped_for_deadline"], 1)
        self.assertGreaterEqual(len(stamps), 10)
        self.assertTrue(all(stamp < deadline for stamp in stamps),
                        "a read was started after the deadline: %s" % stamps[-3:])
        self.assertEqual(len(bridge.calls), len(stamps))
        for stamp, call in zip(stamps, bridge.calls):
            self.assertLessEqual(call["timeout"], (deadline - stamp) + 1e-9,
                                 "read timeout %r exceeded the remaining budget" % call["timeout"])
            self.assertLessEqual(call["timeout"], 3.0)
            self.assertEqual(call["command"], "health")
        self.assertFalse(report["open_blend_replayed"])

    def test_transport_failure_with_audit_error_record_reports_load_failed(self):
        evidence = self._evidence("req-open-2", ok=False,
                                  result={"opened": False, "use_scripts": False, "load_ui": False})
        evidence[0]["code"] = "unsaved_changes"
        result, error, bridge = self._post_hoc("req-open-2", evidence, confirm_replace_current=True)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("load_failed:"), error)
        self.assertIn("unsaved_changes", error)
        self.assertEqual(bridge.commands().count("open_blend"), 1)

    def test_evidence_for_another_request_is_ignored(self):
        result, error, _bridge = self._post_hoc(
            "req-open-3", self._evidence("someone-else"), confirm_replace_current=True)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("load_pending:"), error)


# --------------------------------------------------------------------------- #
# Blender side: bridge command open_blend
# --------------------------------------------------------------------------- #
class BridgeCommandTests(unittest.TestCase):
    @unittest.skipUnless(EXISTING_BLEND is not None, "no project .blend available for the command tests")
    def setUp(self):
        self.root = PROJECT
        # The command layer re-checks existence/readability on the real file; the
        # Blender-only parts (container/metadata/operator) stay stubbed.
        self.target = EXISTING_BLEND
        self.api = FakeApi(self.root)
        self.bpy = FakeBpy(filepath=str(self.root / "before.blend"), dirty=False)

    def load(self, api=None, bpy_stub=None):
        module = load_bridge_module(bpy_stub or self.bpy)
        module.register_commands(api or self.api)
        return module

    def command(self, api=None, bpy_stub=None):
        module = self.load(api, bpy_stub)
        return module, (api or self.api).commands["open_blend"]

    def open_ok(self, path=None, **kwargs):
        """Make the operator behave like a real successful load."""
        target = Path(path or self.target)

        def on_call(call_kwargs):
            self.bpy.data.filepath = call_kwargs["filepath"]
            self.bpy.context.scene = FakeScene("Case")
            self.bpy.data.scenes = [FakeScene("Case")]
            self.bpy.data.is_dirty = False

        operator = FakeOperator(on_call=on_call)
        self.bpy.ops.wm.open_mainfile = operator
        module, func = self.command()
        result = func(path=str(target), **kwargs)
        return operator, result

    def expect(self, code, func, **kwargs):
        with self.assertRaises(SideError) as caught:
            func(**kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def test_registration_exposes_one_mutating_command(self):
        self.load()
        self.assertEqual(list(self.api.commands), ["open_blend"])
        meta = self.api.command_meta["open_blend"]
        self.assertIs(meta["mutating"], True)
        self.assertIn("confirm", meta["summary"])

    def test_path_policy_is_two_layered(self):
        _module, func = self.command()
        target = str(self.target)
        self.expect("invalid_argument", func, path=42)
        self.expect("invalid_path", func, path="")
        self.expect("invalid_path", func, path="demo/case.blend", confirm_replace_current=True)
        self.expect("invalid_path", func, path=target + "\x00", confirm_replace_current=True)
        self.expect("unc_path", func, path=r"\\server\share\case.blend", confirm_replace_current=True)
        self.expect("device_path", func, path=r"\\?\D:\case.blend", confirm_replace_current=True)
        self.expect("invalid_path", func, path=str(self.root / "demo" / ".." / "case.blend"),
                    confirm_replace_current=True)
        self.expect("invalid_path", func, path=target + os.sep, confirm_replace_current=True)
        self.expect("path_outside_project", func, path=str(config.WORKSPACE_ROOT / "outside.blend"),
                    confirm_replace_current=True)
        self.expect("invalid_path", func, path=str(self.root / "demo" / "note.txt"),
                    confirm_replace_current=True)
        delegated = self.root / "demo" / "delegated_but_missing.blend"
        self.expect("file_not_found", func, path=str(delegated), confirm_replace_current=True)
        self.assertIn("demo/delegated_but_missing.blend", self.api.resolved,
                      "a valid-looking path is delegated to resolve_project_path")
        # The real target passes the path layer and is stopped by the confirm gate.
        self.expect("confirmation_required", func, path=str(self.target))
        relative = os.path.relpath(str(self.target), str(self.root)).replace(os.sep, "/")
        self.assertIn(relative, self.api.resolved)

    def test_cross_volume_and_rooted_paths(self):
        _module, func = self.command()
        if _IS_WINDOWS:
            cross = _OTHER_DRIVE + os.sep + "AI" + os.sep + "MCP" + os.sep + "Blender" + os.sep + \
                "Project" + os.sep + "demo" + os.sep + "case.blend"
            error = self.expect("path_outside_project", func, path=cross, confirm_replace_current=True)
            self.assertEqual(error.details["reason"], "different_drive")
            self.expect("invalid_path", func, path=os.sep + "AI" + os.sep + "Project" + os.sep + "a.blend",
                        confirm_replace_current=True)
            self.expect("invalid_path", func, path="C:foo" + os.sep + "a.blend",
                        confirm_replace_current=True)
        # A relative path is refused on every platform.
        self.expect("invalid_path", func, path="demo" + os.sep + "a.blend", confirm_replace_current=True)

    def test_missing_directory_never_created_and_missing_file_refused(self):
        _module, func = self.command()
        missing = self.root / "no_such_dir_9f3a" / "case.blend"
        self.expect("file_not_found", func, path=str(missing), confirm_replace_current=True)
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.api.container_calls, [])

    def test_directory_and_unreadable_target(self):
        _module, func = self.command()
        if EXISTING_DIR is not None:
            self.expect("not_a_file", func, path=str(EXISTING_DIR), confirm_replace_current=True)
        with mock.patch("builtins.open", side_effect=PermissionError("injected: read denied")):
            self.expect("unreadable", func, path=str(self.target), confirm_replace_current=True)

    def test_invalid_booleans_are_refused_before_any_read(self):
        _module, func = self.command()
        for label in ("confirm_replace_current", "load_ui"):
            for value in ("true", 1, 0, None, []):
                kwargs = {"path": str(self.target), label: value}
                self.expect("invalid_argument", func, **kwargs)
        self.assertEqual(self.api.container_calls, [])
        self.assertEqual(self.api.metadata_calls, [])

    def test_confirmation_gate_dirty_clean_and_unknown(self):
        _module, func = self.command()
        self.bpy.data.is_dirty = True
        dirty = self.expect("unsaved_changes", func, path=str(self.target))
        self.assertTrue(dirty.details["would_replace_current_file"])
        self.assertEqual(dirty.details["current_filepath"], str(self.root / "before.blend"))
        self.assertIs(dirty.details["is_dirty"], True)

        self.bpy.data.is_dirty = False
        clean = self.expect("confirmation_required", func, path=str(self.target))
        self.assertFalse(clean.details["is_dirty"])
        self.assertTrue(clean.details["would_replace_current_file"])

        del self.bpy.data.is_dirty
        unknown = self.expect("confirmation_required", func, path=str(self.target))
        self.assertIsNone(unknown.details["is_dirty"])
        self.assertFalse(unknown.details["is_dirty_known"])
        self.assertEqual(self.api.container_calls, [], "no preflight before confirmation")

    def test_confirmation_gate_does_not_read_or_write_the_target(self):
        _module, func = self.command()
        before = self.bpy.data.filepath
        self.bpy.data.is_dirty = True
        self.expect("unsaved_changes", func, path=str(self.target))
        self.assertEqual(self.bpy.data.filepath, before)
        self.assertEqual(self.api.container_calls, [])
        self.assertEqual(self.bpy.ops.wm.open_mainfile.calls, [])

    def test_damaged_blend_container_is_refused(self):
        api = FakeApi(self.root, container={"bytes": 8, "container": None, "magic": "00000000"})
        _module, func = self.command(api)
        self.expect("invalid_blend", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(api.metadata_calls, [], "no enumeration after a bad container")

    def test_current_file_preflight_uses_disk_copy_without_saving_live_main(self):
        self.bpy.data.filepath = str(self.target)
        module, func = self.command()
        scratch = ROOT / ".runtime" / "unit-mocked-open-preflight.blend"
        with mock.patch.object(module.tempfile, "mkstemp", return_value=(9876, str(scratch))) as create, \
             mock.patch.object(module.os, "close") as close, \
             mock.patch.object(module.shutil, "copyfile") as copy, \
             mock.patch.object(Path, "unlink") as unlink:
            result = func(path=str(self.target), confirm_replace_current=True)
        copy.assert_called_once_with(self.target, scratch)
        close.assert_called_once_with(9876)
        self.assertEqual(Path(create.call_args.kwargs["dir"]), ROOT / ".runtime")
        unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(self.api.metadata_calls, [str(scratch)])
        self.assertEqual(result["preflight"]["inspection_mode"], "temporary_disk_copy")
        self.assertFalse(result["saved_before_open"])
        self.assertFalse(result["target_written"])

    def test_current_file_parse_failure_cleans_copy_and_does_not_open(self):
        self.bpy.data.filepath = str(self.target)
        api = FakeApi(self.root, metadata_error=SideError("blend_inspect_failed", "corrupt inventories"))
        module, func = self.command(api=api)
        scratch = ROOT / ".runtime" / "unit-mocked-open-preflight.blend"
        with mock.patch.object(module.tempfile, "mkstemp", return_value=(9876, str(scratch))), \
             mock.patch.object(module.os, "close"), \
             mock.patch.object(module.shutil, "copyfile"), \
             mock.patch.object(Path, "unlink") as unlink:
            self.expect("blend_parse_failed", func, path=str(self.target), confirm_replace_current=True)
        unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(self.bpy.ops.wm.open_mainfile.calls, [])
        self.assertEqual(self.bpy.ops.wm.open_mainfile.calls, [])

    def test_blend_that_blender_cannot_parse_is_refused(self):
        api = FakeApi(self.root, metadata_error=SideError("blend_inspect_failed", "corrupt inventories"))
        _module, func = self.command(api)
        error = self.expect("blend_parse_failed", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(error.details["code"], "blend_inspect_failed")
        self.assertEqual(self.bpy.ops.wm.open_mainfile.calls, [])

    def test_preflight_api_missing_is_unsupported(self):
        api = FakeApi(self.root)
        api._temp_blend_metadata = None
        _module, func = self.command(api)
        self.expect("unsupported_api", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(self.bpy.ops.wm.open_mainfile.calls, [])

        api = FakeApi(self.root, metadata_error=SideError("api_unsupported", "no temp_data"))
        _module, func = self.command(api)
        self.expect("unsupported_api", func, path=str(self.target), confirm_replace_current=True)

    def test_operator_api_must_expose_exact_properties(self):
        cases = (
            (None, "unsupported_api"),                       # no operator at all
            ({}, "unsupported_api"),                         # no properties visible
            ({"filepath": "STRING", "load_ui": "BOOLEAN"}, "unsupported_api"),   # no use_scripts
            ({"filepath": "STRING", "load_ui": "BOOLEAN", "use_scripts": "STRING"}, "unsupported_api"),
        )
        for properties, code in cases:
            bpy = FakeBpy(filepath=str(self.root / "before.blend"))
            bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(
                open_mainfile=None if properties is None else FakeOperator(properties=properties)))
            api = FakeApi(self.root)
            _module, func = self.command(api, bpy)
            self.expect(code, func, path=str(self.target), confirm_replace_current=True)
            if properties is not None:
                self.assertEqual(bpy.ops.wm.open_mainfile.calls, [])
            self.assertEqual(api.metadata_calls, [str(self.target)])

    def test_successful_open_payload_and_operator_arguments(self):
        operator, result = self.open_ok(confirm_replace_current=True)
        self.assertEqual(len(operator.calls), 1)
        call = operator.calls[0]
        self.assertEqual(call["args"], ("EXEC_DEFAULT",))
        self.assertEqual(call["kwargs"], {"filepath": str(self.target), "load_ui": False, "use_scripts": False})
        self.assertTrue(result["opened"])
        self.assertEqual(result["path"], str(self.target))
        self.assertEqual(result["use_scripts"], False)
        self.assertIs(result["load_ui"], False)
        self.assertEqual(result["exec_context"], "EXEC_DEFAULT")
        self.assertEqual(result["current"]["scene"], "Case")
        self.assertEqual(result["previous"]["blend_filepath"], str(self.root / "before.blend"))
        self.assertFalse(result["saved_before_open"])
        self.assertFalse(result["target_written"])
        self.assertEqual(result["rollback"], "not_attempted")
        self.assertTrue(result["preflight"]["read_only"])
        self.assertEqual(result["preflight"]["container"]["container"], "blend")
        self.assertEqual(result["preflight"]["counts"]["objects"], 1)
        self.assertEqual(result["blender_version"], "5.2.1")

    def test_load_ui_true_is_forwarded(self):
        operator, result = self.open_ok(confirm_replace_current=True, load_ui=True)
        self.assertIs(operator.calls[0]["kwargs"]["load_ui"], True)
        self.assertIs(result["load_ui"], True)
        self.assertIs(result["ui_mode"]["load_ui"], True)

    def test_non_finished_operator_result_is_load_failed(self):
        self.bpy.ops.wm.open_mainfile = FakeOperator(result=("CANCELLED",))
        _module, func = self.command()
        error = self.expect("load_failed", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(error.details["stage"], "operator_result")
        self.assertEqual(error.details["operator_result"], ["CANCELLED"])

    def test_operator_exception_reports_honest_state_without_rollback(self):
        self.bpy.ops.wm.open_mainfile = FakeOperator(raise_error=RuntimeError("injected load crash"))
        _module, func = self.command()
        error = self.expect("load_failed", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(error.details["stage"], "exec")
        self.assertFalse(error.details["current_filepath_changed"])
        self.assertEqual(error.details["rollback"], "not_attempted")
        self.assertIn("state", error.details)

    def test_untitled_session_is_not_reported_as_a_path_change(self):
        # '' is Blender's unnamed session; '' -> '' is NOT a filepath change.
        for operator in (FakeOperator(raise_error=RuntimeError("crash")),
                         FakeOperator(result=("CANCELLED",))):
            bpy_stub = FakeBpy(filepath="", dirty=False)
            bpy_stub.ops.wm.open_mainfile = operator
            api = FakeApi(self.root)
            _module, func = self.command(api, bpy_stub)
            error = self.expect("load_failed", func, path=str(self.target), confirm_replace_current=True)
            self.assertFalse(error.details["current_filepath_changed"],
                             "%s wrongly claimed the filepath changed" % error.details["stage"])
            self.assertEqual(error.details["current_filepath"], "")

    def test_untitled_session_that_becomes_named_is_a_path_change(self):
        bpy_stub = FakeBpy(filepath="", dirty=False)

        def on_call(call_kwargs):
            bpy_stub.data.filepath = str(self.root / "elsewhere.blend")

        bpy_stub.ops.wm.open_mainfile = FakeOperator(on_call=on_call)
        _module, func = self.command(FakeApi(self.root), bpy_stub)
        error = self.expect("load_failed", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(error.details["stage"], "verify_filepath")
        self.assertTrue(error.details["current_filepath_changed"])

    def test_preflight_dirty_flag_drift_is_refused_before_open(self):
        # A read-only preflight that flips bpy.data.is_dirty is an observable change
        # and must abort the open instead of being hidden.
        bpy_stub = FakeBpy(filepath=str(self.root / "before.blend"), dirty=False)

        def flip_dirty():
            bpy_stub.data.is_dirty = True

        api = FakeApi(self.root, on_metadata=flip_dirty)
        operator = FakeOperator()
        bpy_stub.ops.wm.open_mainfile = operator
        _module, func = self.command(api, bpy_stub)
        error = self.expect("preflight_changed_state", func, path=str(self.target),
                            confirm_replace_current=True)
        self.assertIn("is_dirty", error.details["delta"])
        self.assertEqual(error.details["delta"]["is_dirty"], {"before": False, "after": True})
        self.assertEqual(operator.calls, [], "the operator must not run after a preflight drift")
        self.assertEqual(api.container_calls, [str(self.target)])

    def test_preflight_fingerprint_is_reused_and_drift_is_detected(self):
        bpy_stub = FakeBpy(filepath=str(self.root / "before.blend"), dirty=False)

        def fingerprint():
            return {"blend_filepath": bpy_stub.data.filepath, "is_dirty": bpy_stub.data.is_dirty,
                    "objects_total": 0, "save_version": 3}

        # No drift: the report must state which proof was used and what was compared.
        api = FakeApi(self.root, fingerprint=fingerprint)
        bpy_stub.ops.wm.open_mainfile = FakeOperator(on_call=lambda kwargs: setattr(
            bpy_stub.data, "filepath", kwargs["filepath"]))
        _module, func = self.command(api, bpy_stub)
        result = func(path=str(self.target), confirm_replace_current=True)
        self.assertEqual(result["preflight"]["proved_by"], "api._live_state_fingerprint")
        self.assertIn("save_version", result["preflight"]["compared"])
        self.assertIn("is_dirty", result["preflight"]["compared"])
        self.assertTrue(result["preflight"]["live_state_unchanged"])

        # Drift only visible in the fingerprint (a key the local snapshot lacks).
        bpy_stub2 = FakeBpy(filepath=str(self.root / "before.blend"), dirty=False)
        state = {"save_version": 3}

        def fingerprint2():
            return {"blend_filepath": bpy_stub2.data.filepath, "is_dirty": bpy_stub2.data.is_dirty,
                    "save_version": state["save_version"]}

        def bump():
            state["save_version"] = 4

        operator2 = FakeOperator()
        bpy_stub2.ops.wm.open_mainfile = operator2
        api2 = FakeApi(self.root, fingerprint=fingerprint2, on_metadata=bump)
        _module, func = self.command(api2, bpy_stub2)
        error = self.expect("preflight_changed_state", func, path=str(self.target),
                            confirm_replace_current=True)
        self.assertEqual(error.details["delta"]["save_version"], {"before": 3, "after": 4})
        self.assertEqual(error.details["proved_by"], "api._live_state_fingerprint")
        self.assertEqual(operator2.calls, [])

    def test_preflight_fallback_proof_does_not_overclaim(self):
        _operator, result = self.open_ok(confirm_replace_current=True)
        self.assertTrue(result["preflight"]["proved_by"].startswith("local scalar snapshot"))
        self.assertIn("is_dirty", result["preflight"]["compared"])
        self.assertIn("blend_filepath", result["preflight"]["compared"])

    def test_filepath_mismatch_after_finished_is_refused(self):
        def on_call(_kwargs):
            self.bpy.data.filepath = str(self.root / "demo" / "somewhere_else.blend")

        self.bpy.ops.wm.open_mainfile = FakeOperator(on_call=on_call)
        _module, func = self.command()
        error = self.expect("load_failed", func, path=str(self.target), confirm_replace_current=True)
        self.assertEqual(error.details["stage"], "verify_filepath")
        self.assertTrue(error.details["current_filepath_changed"])
        self.assertEqual(error.details["actual"], str(self.root / "demo" / "somewhere_else.blend"))

    def test_preflight_and_snapshot_keep_no_rna_references(self):
        _operator, result = self.open_ok(confirm_replace_current=True)
        for section in ("previous", "current", "ui_mode", "preflight"):
            for value in json.loads(json.dumps(result[section], default=str)).values():
                self.assertNotIn("<bpy", str(value))
        self.assertEqual(self.api.metadata_calls, [str(self.target)])
        self.assertEqual(self.api.container_calls, [str(self.target)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
