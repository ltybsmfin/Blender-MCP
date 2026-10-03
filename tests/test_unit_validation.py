"""Unit and validation tests for the Blender MCP server and the Blender add-on.

These tests need neither Blender nor a GPU: the add-on module is imported with a
stub ``bpy`` module so its *pure* logic (framing, token handling, path safety,
driver-expression validation, socket type rules, dispatch validation) can be
exercised directly, and the bridge client is tested against an in-process fake
bridge that speaks the same protocol.

Run::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_unit_validation.py

A JSON report is written to ``MCP/tests/reports/unit_report.json``.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
WORKSPACE = MCP_DIR.parent
REPORT_DIR = TESTS_DIR / "reports"
TMP_ROOT = MCP_DIR / ".runtime" / "test_tmp"

sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(MCP_DIR / "blender"))


# --------------------------------------------------------------------------- #
# stub bpy so the self-contained add-on module can be imported without Blender
# --------------------------------------------------------------------------- #
def _install_bpy_stub() -> None:
    if "bpy" in sys.modules:
        return
    bpy = types.ModuleType("bpy")
    bpy_types = types.ModuleType("bpy.types")

    class _Operator:  # minimal stand-ins for the RNA base classes
        pass

    class _Panel:
        pass

    class _EmptyCollection:
        """Stands in for a bpy_prop_collection so lookups raise like Blender does."""

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
    for collection in ("objects", "node_groups", "scenes", "collections", "meshes", "materials", "shape_keys"):
        setattr(bpy_data, collection, _EmptyCollection())
    bpy.data = bpy_data
    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy_types


_install_bpy_stub()

import blender_mcp_addon as addon  # noqa: E402  (needs the stub first)

from blender_mcp import config  # noqa: E402
from blender_mcp.bridge import (  # noqa: E402
    BlenderCommandError,
    BridgeClient,
    BridgeProtocolError,
    BridgeTimeout,
    BridgeUnavailable,
)


# --------------------------------------------------------------------------- #
# fake bridge used by the client tests
# --------------------------------------------------------------------------- #
class FakeBridge:
    """Minimal server speaking the same newline-delimited JSON protocol."""

    def __init__(self, behaviour, token: str = "unit-test-token"):
        self.behaviour = behaviour
        self.token = token
        self.requests: list[dict] = []
        self._stop = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buffer = b""
        with conn:
            while b"\n" not in buffer:
                try:
                    chunk = conn.recv(65536)
                except (OSError, socket.timeout):
                    return
                if not chunk:
                    return
                buffer += chunk
            line = buffer.split(b"\n", 1)[0]
            try:
                request = json.loads(line.decode("utf-8"))
            except ValueError:
                conn.sendall(b'{"id": null, "ok": false, "error": {"code": "bad_request"}}\n')
                return
            self.requests.append(request)
            answer = self.behaviour(request, len(self.requests))
            if answer is None:  # behaviour: stay silent (timeout test)
                time.sleep(2.0)
                return
            if isinstance(answer, bytes):
                conn.sendall(answer)
                return
            conn.sendall(json.dumps(answer).encode("utf-8") + b"\n")


def ok_response(request, result=None):
    return {"id": request["id"], "ok": True, "result": result or {"pong": True}}


# --------------------------------------------------------------------------- #
class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.project = config.project_dir()
        self.project.mkdir(parents=True, exist_ok=True)

    def test_accepts_plain_relative_path(self):
        resolved = config.resolve_project_path("demo/scene.blend")
        self.assertTrue(str(resolved).lower().endswith("demo\\scene.blend") or str(resolved).endswith("demo/scene.blend"))
        self.assertTrue(str(resolved).startswith(str(self.project.resolve())))

    def test_accepts_forward_slashes(self):
        resolved = config.resolve_project_path("demo/sub/scene.blend")
        self.assertIn("sub", str(resolved))

    def test_rejects_empty(self):
        with self.assertRaises(config.PathValidationError):
            config.resolve_project_path("")

    def test_rejects_traversal(self):
        for bad in ("../escape.blend", "demo/../../escape.blend", "..\\escape.blend"):
            with self.assertRaises(config.PathValidationError, msg=bad):
                config.resolve_project_path(bad)

    def test_rejects_absolute(self):
        absolute = str(self.project / "demo" / "x.blend")
        with self.assertRaises(config.PathValidationError):
            config.resolve_project_path(absolute)

    def test_addon_side_matches_server_side_rules(self):
        for good in ("demo/a.blend", "a.blend", "demo/x/y.blend"):
            self.assertEqual(
                str(addon.resolve_project_path(good)).lower(),
                str(config.resolve_project_path(good)).lower(),
            )
        for bad in ("../a.blend", "", "C:\\Windows\\evil.blend", "a/../../b.blend"):
            with self.assertRaises(addon.CommandError, msg=bad):
                addon.resolve_project_path(bad)

    def test_rejects_symlinked_component_when_available(self):
        import subprocess

        link = self.project / "link_dir"
        target = self.project / "real_dir"
        target.mkdir(parents=True, exist_ok=True)

        def cleanup():
            for candidate in (link, target):
                try:
                    if candidate.is_symlink() or candidate.exists():
                        os.rmdir(candidate)
                except OSError:
                    pass

        self.addCleanup(cleanup)
        if link.exists() or link.is_symlink():
            os.rmdir(link)
        created = False
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            # Windows needs a privilege for symlinks; a directory junction is an
            # equally valid reparse point and needs none.
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            created = result.returncode == 0
        if not created:
            self.skipTest("cannot create a symlink or junction in this environment")

        self.assertTrue(addon._is_reparse_point(link), "the link must be detected as a reparse point")
        with self.assertRaises(config.PathValidationError):
            config.resolve_project_path("link_dir/evil.blend")
        with self.assertRaises(addon.CommandError):
            addon.resolve_project_path("link_dir/evil.blend")


class TokenTests(unittest.TestCase):
    def test_token_is_created_stable_and_not_leaked_in_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            first = addon.read_or_create_token(path)
            second = addon.read_or_create_token(path)
            self.assertEqual(first, second)
            self.assertEqual(len(first), addon.TOKEN_BYTES * 2)
            self.assertRegex(first, r"^[0-9a-f]+$")

    def test_config_ensure_token_matches_addon_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t"
            self.assertEqual(config.ensure_token(path), config.read_token(path))
            self.assertEqual(config.ensure_token(path), addon.read_or_create_token(path))


class JsonAndSocketTests(unittest.TestCase):
    def test_jsonable_handles_nested_and_duck_typed_values(self):
        class Vec:
            def to_tuple(self):
                return (1.0, 2.0, 3.0)

        payload = addon._jsonable({"a": [1, (2.0, 3.0)], "v": Vec(), "n": float("nan")})
        self.assertEqual(payload["a"], [1, [2.0, 3.0]])
        self.assertEqual(payload["v"], [1.0, 2.0, 3.0])
        self.assertEqual(payload["n"], "nan")

    def test_socket_type_compatibility(self):
        class Sock:
            def __init__(self, type_name):
                self.type = type_name

        self.assertTrue(addon._socket_types_compatible(Sock("GEOMETRY"), Sock("GEOMETRY")))
        self.assertTrue(addon._socket_types_compatible(Sock("FLOAT"), Sock("VECTOR")))
        self.assertTrue(addon._socket_types_compatible(Sock("INT"), Sock("FLOAT")))
        self.assertFalse(addon._socket_types_compatible(Sock("GEOMETRY"), Sock("FLOAT")))
        self.assertFalse(addon._socket_types_compatible(Sock("STRING"), Sock("FLOAT")))

    def test_set_socket_value_coerces_and_reports_readback(self):
        class Sock:
            def __init__(self, value, type_name):
                self.default_value = value
                self.type = type_name
                self.identifier = "Socket_0"
                self.name = "Probe"

        scalar = Sock(0.0, "FLOAT")
        self.assertEqual(addon.set_socket_value(scalar, 3), 3.0)
        self.assertIsInstance(scalar.default_value, float)

        integer = Sock(0, "INT")
        self.assertEqual(addon.set_socket_value(integer, 7), 7)

        vector = Sock([0.0, 0.0, 0.0], "VECTOR")
        self.assertEqual(addon.set_socket_value(vector, [1.0, 2.0]), [1.0, 2.0, 0.0])

        text = Sock("", "STRING")
        self.assertEqual(addon.set_socket_value(text, 5), "5")

        with self.assertRaises(addon.CommandError):
            addon.set_socket_value(Sock(0.0, "FLOAT"), None)


class DriverExpressionTests(unittest.TestCase):
    def test_accepts_arithmetic_and_declared_variables(self):
        result = addon._validate_driver_expression("phase * 2 + sin(pi)", ["phase"])
        self.assertEqual(result["declared_variables"], ["phase"])

    def test_accepts_builtin_only_expression(self):
        addon._validate_driver_expression("1 + 2 * 3", [])

    def test_accepts_large_literal_exponents(self):
        addon._validate_driver_expression("1e3 + 2.5", [])

    def test_rejects_unknown_variable(self):
        with self.assertRaises(addon.CommandError) as ctx:
            addon._validate_driver_expression("phas * 2", ["phase"])
        self.assertEqual(ctx.exception.code, "unknown_variable")

    def test_rejects_python_escapes(self):
        for expression in (
            "().__class__",
            "__import__('os')",
            "open('x')",
            "lambda: 1",
            "eval('1')",
            "exec('1')",
            "1; 2",
            "print(1)",
        ):
            with self.assertRaises(addon.CommandError, msg=expression) as ctx:
                addon._validate_driver_expression(expression, [])
            self.assertIn(ctx.exception.code, ("invalid_expression", "unknown_variable"))

    def test_rejects_empty(self):
        with self.assertRaises(addon.CommandError):
            addon._validate_driver_expression("   ", [])


class DispatchValidationTests(unittest.TestCase):
    """Parameter validation must happen before any bpy object is touched."""

    def test_unknown_parameter_is_explicit(self):
        with self.assertRaises(addon.CommandError) as ctx:
            addon.dispatch("create_mesh_object", {"name": "x", "vertices": [[0, 0, 0]], "bogus": 1})
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn("bogus", ctx.exception.message)

    def test_missing_required_parameter_is_explicit(self):
        with self.assertRaises(addon.CommandError) as ctx:
            addon.dispatch("create_mesh_object", {})
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn("missing required", ctx.exception.message)
        self.assertIn("vertices", ctx.exception.details["accepted"])

    def test_unknown_command_lists_available(self):
        with self.assertRaises(addon.CommandError) as ctx:
            addon.dispatch("not_a_command", {})
        self.assertEqual(ctx.exception.code, "unknown_command")
        self.assertIn("create_mesh_object", ctx.exception.details["available"])

    def test_registry_covers_the_required_commands(self):
        required = {
            "health", "scene_summary", "list_objects", "object_info", "create_mesh_object",
            "set_custom_property", "create_node_group", "create_node", "set_node_property",
            "set_socket_default", "link_sockets", "unlink_socket", "remove_node",
            "inspect_node_group", "attach_geometry_nodes_modifier", "set_modifier_input",
            "list_node_types", "describe_node_type", "batch_graph_edit", "inspect_shape_keys",
            "create_shape_key", "set_shape_key_coordinates", "set_shape_key", "remove_shape_key",
            "inspect_drivers", "add_driver", "update_driver", "remove_driver", "save_blend",
        }
        missing = required - set(addon._COMMANDS)
        self.assertEqual(missing, set(), "missing bridge commands: %s" % sorted(missing))

    def test_no_python_execution_command_exists(self):
        """There must be no eval/exec style bridge command."""
        for name in addon._COMMANDS:
            lowered = name.lower()
            self.assertNotIn("eval", lowered)
            self.assertNotIn("exec", lowered)
            self.assertNotIn("python", lowered)


class BridgeServerProtocolTests(unittest.TestCase):
    """The real non-blocking server, driven directly with push/poll."""

    TOKEN = "server-protocol-token"

    def setUp(self):
        self.logs: list[str] = []
        self.server = addon.BridgeServer("127.0.0.1", 0, self.TOKEN, self.logs.append)
        self.server.start()
        self.port = self.server._sock.getsockname()[1]

    def tearDown(self):
        self.server.stop("test teardown")

    def _exchange(self, raw: bytes, expect_response: bool = True, timeout: float = 3.0):
        """Send raw bytes, pump the server, read one response line."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(raw)
            deadline = time.time() + timeout
            buffer = b""
            while time.time() < deadline:
                self.server.poll()
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if b"\n" in buffer:
                    break
            if not expect_response:
                return buffer
            if b"\n" not in buffer:
                self.fail("no complete response line (buffer=%r)" % buffer)
            return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    @staticmethod
    def _request(command: str, params: dict | None = None, token: str = TOKEN, request_id: str = "r1") -> bytes:
        return (
            json.dumps({"id": request_id, "token": token, "command": command, "params": params or {}}) + "\n"
        ).encode("utf-8")

    def test_ping_round_trip(self):
        response = self._exchange(self._request("ping"))
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["result"]["pong"])
        self.assertEqual(response["protocol"], addon.PROTOCOL_VERSION)

    def test_list_commands_round_trip(self):
        response = self._exchange(self._request("list_commands"))
        names = {entry["name"] for entry in response["result"]["commands"]}
        self.assertIn("create_node_group", names)
        self.assertIn("add_driver", names)

    def test_wrong_token_is_unauthorized_and_connection_closes(self):
        response = self._exchange(self._request("ping", token="definitely-wrong"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "unauthorized")
        self.assertEqual(self.server.unauthorized, 1)
        self.assertNotIn("definitely-wrong", json.dumps(response))

    def test_missing_token_is_unauthorized(self):
        raw = (json.dumps({"id": "x", "command": "ping", "params": {}}) + "\n").encode("utf-8")
        response = self._exchange(raw)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "unauthorized")

    def test_malformed_json_is_rejected(self):
        response = self._exchange(b"{not json at all}\n")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")

    def test_non_object_json_is_rejected(self):
        response = self._exchange(b"[1, 2, 3]\n")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "bad_request")

    def test_unknown_command_is_rejected(self):
        response = self._exchange(self._request("definitely_not_a_command"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "unknown_command")

    def test_command_parameter_validation_through_the_wire(self):
        response = self._exchange(self._request("create_mesh_object", {"name": "x"}))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_argument")

    def test_command_error_is_reported_without_crashing_the_server(self):
        response = self._exchange(self._request("object_info", {"name": "no-such-object"}))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "object_not_found")
        # The server survives and still answers.
        second = self._exchange(self._request("ping", request_id="r2"))
        self.assertTrue(second["ok"])

    def test_oversized_frame_is_refused(self):
        # A dedicated server with a tiny cap so the test does not have to push
        # 8 MiB through the socket (sendall would block until the poll loop reads).
        small = addon.BridgeServer("127.0.0.1", 0, self.TOKEN, self.logs.append, max_frame=1024)
        small.start()
        self.addCleanup(lambda: small.stop("test teardown"))
        port = small._sock.getsockname()[1]
        raw = (
            b'{"token": "' + self.TOKEN.encode() + b'", "command": "ping", "id": "big", "pad": "'
            + b"a" * 8192 + b'"}\n'
        )
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(raw)
            buffer = b""
            deadline = time.time() + 5.0
            while time.time() < deadline and b"\n" not in buffer:
                small.poll()
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    continue
                if not chunk:
                    break
                buffer += chunk
        self.assertIn(b"\n", buffer, "server must answer an oversized frame with an error")
        response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "frame_too_large")

    def test_oversized_codec_cap_is_configurable(self):
        self.assertEqual(self.server.max_frame, addon.MAX_FRAME_BYTES)

    def test_server_status_reports_counters(self):
        self._exchange(self._request("ping"))
        status = self.server.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["requests_ok"], 1)
        self.assertNotIn(self.TOKEN, json.dumps(status))

    def test_bind_conflict_is_reported(self):
        clash = addon.BridgeServer("127.0.0.1", self.port, self.TOKEN, self.logs.append)
        with self.assertRaises(addon.CommandError) as ctx:
            clash.start()
        self.assertEqual(ctx.exception.code, "bind_failed")


class BridgeClientTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._servers: list[FakeBridge] = []

    def tearDown(self):
        for server in self._servers:
            server.stop()

    def _client(self, behaviour, token="unit-test-token", timeout=3.0) -> tuple[BridgeClient, FakeBridge]:
        server = FakeBridge(behaviour, token=token).start()
        self._servers.append(server)
        client = BridgeClient(host=server.host, port=server.port, token=token, timeout=timeout)
        return client, server

    def test_happy_path(self):
        client, server = self._client(lambda request, count: ok_response(request))
        result = client.call("ping", {})
        self.assertEqual(result["pong"], True)
        self.assertEqual(server.requests[0]["token"], "unit-test-token")

    def test_remote_command_error_is_typed(self):
        def behaviour(request, count):
            return {
                "id": request["id"],
                "ok": False,
                "error": {"code": "topology_mismatch", "message": "counts differ", "details": {"a": 1}},
            }

        client, _ = self._client(behaviour)
        with self.assertRaises(BlenderCommandError) as ctx:
            client.call("set_shape_key_coordinates", {})
        self.assertEqual(ctx.exception.remote_code, "topology_mismatch")
        self.assertIn("counts differ", ctx.exception.as_tool_message())
        self.assertIn('"a": 1', ctx.exception.as_tool_message())

    def test_bad_json_answer_is_protocol_error(self):
        client, _ = self._client(lambda request, count: b"this is not json\n")
        with self.assertRaises(BridgeProtocolError):
            client.call("ping", {})

    def test_id_mismatch_is_protocol_error(self):
        client, _ = self._client(lambda request, count: {"id": "some-other-id", "ok": True, "result": {}})
        with self.assertRaises(BridgeProtocolError):
            client.call("ping", {})

    def test_oversized_answer_is_protocol_error(self):
        client, _ = self._client(
            lambda request, count: b'{"id": "' + request["id"].encode() + b'", "ok": true, "result": "' + b"x" * 5000 + b'"}\n'
        )
        client.max_frame = 512
        with self.assertRaises(BridgeProtocolError):
            client.call("ping", {})

    def test_timeout_does_not_replay_the_request(self):
        client, server = self._client(lambda request, count: None, timeout=0.5)
        with self.assertRaises(BridgeTimeout):
            client.call("save_blend", {"relative_path": "demo/x.blend"})
        time.sleep(0.3)
        self.assertEqual(len(server.requests), 1, "a mutating request must never be replayed")

    def test_timeout_releases_the_connection_and_partial_frame(self):
        """A timed-out call must drop socket AND buffered bytes (no stale answers)."""
        state = {"mode": "silent"}

        def behaviour(request, count):
            if state["mode"] == "silent":
                return None
            return ok_response(request, {"pong": True, "mode": state["mode"]})

        client, server = self._client(behaviour, timeout=0.5)
        with self.assertRaises(BridgeTimeout):
            client.call("ping", {})
        self.assertIsNone(client._sock, "the socket must be released after a timeout")
        self.assertEqual(client._buffer, b"", "a partially read frame must be discarded")

        # The next call reconnects from scratch and gets the right answer.
        state["mode"] = "answered"
        result = client.call("ping", {})
        self.assertEqual(result["mode"], "answered")
        self.assertEqual(len(server.requests), 2)

    def test_protocol_error_releases_the_connection(self):
        client, _ = self._client(lambda request, count: b"not json at all\n")
        with self.assertRaises(BridgeProtocolError):
            client.call("ping", {})
        self.assertIsNone(client._sock)

    def test_connection_refused_is_unavailable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        client = BridgeClient(host="127.0.0.1", port=port, token="t", timeout=1.0)
        with self.assertRaises(BridgeUnavailable) as ctx:
            client.call("ping", {})
        self.assertIn("bootstrap", ctx.exception.message)

    def test_request_frame_cap(self):
        client, _ = self._client(lambda request, count: ok_response(request))
        client.max_frame = 256
        with self.assertRaises(BridgeProtocolError):
            client.call("create_mesh_object", {"vertices": [[0.0, 0.0, 0.0]] * 100})

    def test_missing_token_file_is_reported(self):
        client = BridgeClient(host="127.0.0.1", port=1, token=None, timeout=0.5)
        original = config.read_token
        original_ensure = config.ensure_token
        config.read_token = lambda path=None: None  # type: ignore[assignment]
        config.ensure_token = lambda path=None: ""  # type: ignore[assignment]
        try:
            with self.assertRaises(BridgeUnavailable) as ctx:
                client.call("ping", {})
            self.assertIn("token", ctx.exception.message)
        finally:
            config.read_token = original  # type: ignore[assignment]
            config.ensure_token = original_ensure  # type: ignore[assignment]

    def test_healthy_flag(self):
        client, _ = self._client(lambda request, count: ok_response(request, {"blender_version": "5.2.1 LTS"}))
        healthy, payload = client.is_healthy(timeout=2.0)
        self.assertTrue(healthy)
        self.assertEqual(payload["blender_version"], "5.2.1 LTS")


class ServerToolSurfaceTests(unittest.TestCase):
    """The MCP surface must expose the required tools and a typed schema."""

    @classmethod
    def setUpClass(cls):
        import asyncio

        from blender_mcp.tools import build_server

        cls.mcp = build_server()
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.mcp.list_tools())}

    def test_required_tools_present(self):
        required = {
            "blender_health", "blender_scene_summary", "blender_list_objects", "blender_object_info",
            "blender_create_mesh_object", "blender_set_custom_property", "blender_create_node_group",
            "blender_create_node", "blender_set_node_property", "blender_set_socket_default",
            "blender_link_sockets", "blender_unlink_socket", "blender_remove_node",
            "blender_inspect_node_group", "blender_attach_geometry_nodes_modifier",
            "blender_set_modifier_input", "blender_list_node_types", "blender_describe_node_type",
            "blender_batch_graph_edit", "blender_inspect_shape_keys", "blender_create_shape_key",
            "blender_set_shape_key_coordinates", "blender_set_shape_key", "blender_remove_shape_key",
            "blender_inspect_drivers", "blender_add_driver", "blender_update_driver",
            "blender_remove_driver", "blender_save_blend", "blender_launch_blender",
        }
        missing = required - set(self.tools)
        self.assertEqual(missing, set(), "missing MCP tools: %s" % sorted(missing))

    def test_no_eval_tool(self):
        for name in self.tools:
            lowered = name.lower()
            self.assertNotIn("eval", lowered)
            self.assertNotIn("exec", lowered)
            self.assertNotIn("python", lowered)

    def test_instructions_mention_core_rules(self):
        instructions = self.tools and self.mcp.instructions or ""
        for phrase in ("geometry nodes", "identifier", "topology", "dependency-graph", "Project"):
            self.assertIn(phrase, instructions, "instructions must mention %r" % phrase)

    def test_schemas_are_typed(self):
        health = self.tools["blender_object_info"].inputSchema
        self.assertEqual(health["properties"]["name"]["type"], "string")
        self.assertIn("include_evaluated", health["properties"])
        shapes = self.tools["blender_set_shape_key_coordinates"].inputSchema
        self.assertEqual(shapes["properties"]["object"]["type"], "string")
        batch = self.tools["blender_batch_graph_edit"].inputSchema
        self.assertIn("operations", batch["properties"])
        items = batch["properties"]["operations"]["items"]
        # FastMCP references the pydantic model instead of inlining it.
        reference = items.get("$ref", items.get("type"))
        self.assertTrue(
            reference == "object" or str(reference).startswith("#/$defs/"),
            "operations items should be a typed object, got %r" % (items,),
        )
        if str(reference).startswith("#/$defs/"):
            definition = batch["$defs"][str(reference).split("/")[-1]]
            self.assertIn("create_node", definition["properties"]["op"]["enum"])
            self.assertTrue(definition.get("additionalProperties"))
        drivers = self.tools["blender_add_driver"].inputSchema
        self.assertIn("variables", drivers["properties"])
        self.assertEqual(drivers["properties"]["owner_kind"].get("enum"), ["object", "shape_keys", "node_group", "scene"])

    def test_every_tool_has_a_description(self):
        for name, tool in self.tools.items():
            self.assertTrue(tool.description, "tool %s has no description" % name)
            self.assertGreater(len(tool.description), 40, "tool %s description is too terse" % name)


class ConfigTests(unittest.TestCase):
    def test_blender_executable_discovery(self):
        found = config.resolve_blender_executable()
        self.assertTrue(found.is_file(), found)
        self.assertEqual(found.name.lower(), "blender.exe")

    def test_environment_description_is_secret_free(self):
        described = json.dumps(config.describe_environment())
        token = config.read_token()
        if token:
            self.assertNotIn(token, described)

    def test_bridge_address_defaults(self):
        self.assertEqual(config.bridge_host(), "127.0.0.1")
        self.assertEqual(config.bridge_port(), 9876)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    started = time.time()
    result = runner.run(suite)
    payload = {
        "suite": "unit_validation",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "duration_s": round(time.time() - started, 3),
        "tests_run": result.testsRun,
        "failures": [str(test) for test, _ in result.failures],
        "errors": [str(test) for test, _ in result.errors],
        "skipped": [str(test) for test, _ in result.skipped],
        "ok": result.wasSuccessful(),
    }
    report = REPORT_DIR / "unit_report.json"
    report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
