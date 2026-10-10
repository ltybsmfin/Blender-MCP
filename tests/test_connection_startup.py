"""Targeted startup/diagnosis regression tests, with no Blender launch or token reads."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from blender_mcp.bridge import BlenderCommandError, BridgeProtocolError, BridgeTimeout, BridgeUnavailable
from blender_mcp.tools import launcher
from blender_mcp.tools._startup import assert_launch_idle, blender_processes, probe_bridge


HEALTH = {"bridge": "blender-mcp", "protocol": 1, "pid": 991, "blender_version": "5.2.1 LTS"}


def bridge(result=HEALTH):
    stub = MagicMock()
    stub.address, stub.host, stub.port = "127.0.0.1:9876", "127.0.0.1", 9876
    if isinstance(result, Exception):
        stub.health.side_effect = result
    else:
        stub.health.return_value = result
    return stub


def launch_tool(stub):
    mcp = FastMCP("startup-test")
    launcher.register(mcp, stub)
    return mcp._tool_manager.get_tool("blender_launch_blender").fn


class DiagnosisTests(unittest.TestCase):
    def test_healthy_identity(self):
        self.assertEqual(probe_bridge(bridge())["stage"], "ready")

    def test_fault_categories_preserved(self):
        cases = [
            (BridgeUnavailable("refused", {"errno": 10061}), "connection", "bridge_unavailable"),
            (BridgeTimeout("busy"), "response_timeout", "bridge_timeout"),
            (BridgeProtocolError("bad frame"), "protocol", "bridge_protocol_error"),
            (BlenderCommandError("health", "unauthorized", "invalid credentials"), "authentication", "unauthorized"),
        ]
        for error, stage, code in cases:
            with self.subTest(stage=stage):
                result = probe_bridge(bridge(error))
                self.assertFalse(result["reachable"])
                self.assertEqual((result["stage"], result["error_code"]), (stage, code))
                self.assertIn(getattr(error, "remote_message", error.message), result["error"])

    def test_wrong_listener_is_not_healthy(self):
        for value in ({"bridge": "other", "protocol": 1}, {"bridge": "blender-mcp", "protocol": 2}, [], None):
            self.assertEqual(probe_bridge(bridge(value))["error_code"], "bridge_protocol_error")


class GuardTests(unittest.TestCase):
    def test_existing_blender_without_bridge_blocks(self):
        with patch("blender_mcp.tools._startup.socket.create_connection", side_effect=ConnectionRefusedError), \
             patch("blender_mcp.tools._startup.blender_processes", return_value=[{"pid": 123}]):
            with self.assertRaisesRegex(ToolError, "blender_without_bridge.*123"):
                assert_launch_idle(bridge(), {"stage": "connection"})

    def test_busy_listener_blocks(self):
        with patch("blender_mcp.tools._startup.socket.create_connection", return_value=MagicMock()):
            with self.assertRaisesRegex(ToolError, "port_in_use"):
                assert_launch_idle(bridge(), {"stage": "connection"})

    def test_unknown_port_state_blocks(self):
        with patch("blender_mcp.tools._startup.socket.create_connection", side_effect=TimeoutError):
            with self.assertRaisesRegex(ToolError, "launch_state_unknown"):
                assert_launch_idle(bridge(), {"stage": "connection"})

    def test_auth_and_timeout_never_start_a_process(self):
        for error in (BridgeTimeout("busy"), BlenderCommandError("health", "unauthorized", "auth")):
            with patch.object(launcher.subprocess, "Popen") as start:
                with self.assertRaisesRegex(ToolError, "launch_blocked"):
                    launch_tool(bridge(error))()
                start.assert_not_called()

    def test_safe_idle_succeeds(self):
        with patch("blender_mcp.tools._startup.socket.create_connection", side_effect=ConnectionRefusedError), \
             patch("blender_mcp.tools._startup.blender_processes", return_value=[]):
            assert_launch_idle(bridge(), {"stage": "connection"})

    def test_process_inventory_failure_is_not_empty(self):
        with patch("blender_mcp.tools._startup.subprocess.run", side_effect=OSError):
            with self.assertRaisesRegex(ToolError, "launch_state_unknown"):
                blender_processes()


class LauncherTests(unittest.TestCase):
    def test_reuses_healthy_bridge_without_launch_or_inventory(self):
        with patch.object(launcher.subprocess, "Popen") as start, patch.object(launcher, "assert_launch_idle") as check:
            result = launch_tool(bridge())()
            self.assertTrue(result["reused"])
            self.assertFalse(result["launched"])
            start.assert_not_called()
            check.assert_not_called()

    def test_forced_duplicate_refused(self):
        with self.assertRaisesRegex(ToolError, "bridge_already_running"):
            launch_tool(bridge())(reuse_existing=False)

    def test_bridge_configuration_override_rejected(self):
        for args in (["--mcp-port", "1234"], ["--mcp-no-timer"], ["--mcp-force"], ["--mcp-pump-seconds", "nan"]):
            with self.subTest(args=args), patch.object(launcher.subprocess, "Popen") as start:
                with self.assertRaises(ToolError):
                    launch_tool(bridge())(extra_args=args)
                start.assert_not_called()

    def test_early_exit_is_an_error_with_recovery_evidence(self):
        process = MagicMock(pid=991)
        process.poll.return_value = 2
        with patch.object(launcher, "assert_launch_idle"), patch.object(launcher.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ToolError, "bridge_start_failed.*PID=991.*exit_code=2.*log="):
                launch_tool(bridge(BridgeUnavailable("refused")))(background=True, wait_seconds=1)
            process.terminate.assert_not_called()
            process.kill.assert_not_called()

    def test_success_has_our_pid_and_pump_configuration(self):
        stub = bridge()
        stub.health.side_effect = [BridgeUnavailable("refused"), HEALTH]
        process = MagicMock(pid=991)
        process.poll.return_value = None
        with patch.object(launcher, "assert_launch_idle"), patch.object(launcher.subprocess, "Popen", return_value=process) as start:
            result = launch_tool(stub)(background=True, wait_seconds=1, extra_args=["--mcp-pump-seconds", "10"])
            self.assertTrue(result["bridge_ready"])
            args = start.call_args.args[0]
            self.assertIn("--mcp-pump", args)
            self.assertIn("--python-exit-code", args)
            self.assertEqual(args[args.index("--mcp-port") + 1], "9876")
            self.assertEqual(result["pid"], HEALTH["pid"])

    def test_concurrent_launch_blocked(self):
        launcher._LAUNCH_LOCK.acquire()
        try:
            with self.assertRaisesRegex(ToolError, "launch_in_progress"):
                launch_tool(bridge())()
        finally:
            launcher._LAUNCH_LOCK.release()


class BootstrapEntryTests(unittest.TestCase):
    def test_successful_gui_entry_returns_without_system_exit(self):
        # Execute only the actual entry block with main replaced by a fake. This
        # checks the process-exit regression without arbitrary bpy execution.
        import ast
        source = Path(__file__).resolve().parents[1] / "blender" / "bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        entry = tree.body[-1]
        module = ast.fix_missing_locations(ast.Module(body=[entry], type_ignores=[]))
        namespace = {"__name__": "__main__", "main": lambda: 0}
        exec(compile(module, str(source), "exec"), namespace)
        self.assertEqual(namespace["status"], 0)
        namespace["main"] = lambda: 2
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            exec(compile(module, str(source), "exec"), namespace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
