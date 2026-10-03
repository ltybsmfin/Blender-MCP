"""Regressions for transport deadlines, configuration boundaries and CLI parsing."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

MCP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_DIR))
from blender_mcp import config
from blender_mcp.bridge import BridgeClient, BridgeTimeout, BridgeUnavailable

spec = importlib.util.spec_from_file_location("review_bootstrap", MCP_DIR / "blender/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class SocketStub:
    def __init__(self, error=None):
        self.error = error
        self.closed = False
        self.writes = 0
        self.timeout = 1.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.writes += 1
        if self.error:
            raise self.error

    def recv(self, count):
        return b"x"

    def close(self):
        self.closed = True


class TransportReviewTests(unittest.TestCase):
    def client(self, sock):
        client = BridgeClient(token="test-token", timeout=1.0)
        client._sock = sock
        return client

    def test_send_failure_closes_without_replaying(self):
        sock = SocketStub(OSError("connection reset"))
        client = self.client(sock)
        with self.assertRaises(BridgeUnavailable):
            client.call("mutation")
        self.assertEqual(sock.writes, 1)
        self.assertTrue(sock.closed)
        self.assertIsNone(client._sock)
        self.assertEqual(client._buffer, b"")

    def test_send_timeout_closes_without_replaying(self):
        sock = SocketStub(socket.timeout("write blocked"))
        client = self.client(sock)
        with self.assertRaises(BridgeTimeout):
            client.call("mutation")
        self.assertEqual(sock.writes, 1)
        self.assertTrue(sock.closed)

    def test_trickle_response_cannot_extend_request_deadline(self):
        sock = SocketStub()
        client = self.client(sock)
        with patch("blender_mcp.bridge.time.monotonic", side_effect=[0.0, 0.4, 0.8, 1.2]):
            with self.assertRaises(BridgeTimeout):
                client.call("mutation")
        self.assertEqual(sock.writes, 1)
        self.assertTrue(sock.closed)
        self.assertEqual(client._buffer, b"")

    def test_loopback_host(self):
        with patch.dict(os.environ, {"BLENDER_MCP_HOST": "localhost"}):
            self.assertEqual(config.bridge_host(), "127.0.0.1")
        for host in ("0.0.0.0", "192.168.1.1", "example.com", "::1"):
            with self.subTest(host=host), patch.dict(os.environ, {"BLENDER_MCP_HOST": host}):
                with self.assertRaises(ValueError):
                    config.bridge_host()

    def test_project_override_keeps_directory_roles(self):
        with patch.dict(os.environ, {"BLENDER_MCP_PROJECT_DIR": str(config.VERSION_DIR)}):
            with self.assertRaises(config.PathValidationError):
                config.project_dir()
        with patch.dict(os.environ, {"BLENDER_MCP_PROJECT_DIR": str(config.PROJECT_DIR / "demo")}):
            self.assertEqual(config.project_dir(), (config.PROJECT_DIR / "demo").resolve())

    def test_missing_selected_blender_does_not_select_another_version(self):
        with patch.dict(os.environ, {"BLENDER_MCP_BLENDER": str(config.VERSION_DIR / "missing/blender.exe")}):
            with self.assertRaises(FileNotFoundError):
                config.resolve_blender_executable()

    def test_headless_duration_consumes_separate_argument(self):
        parsed = bootstrap.parse_args(["--", "--mcp-pump-seconds", "1.25", "--mcp-port", "9876"])
        self.assertEqual(parsed["pump_seconds"], 1.25)
        self.assertEqual(parsed["port"], 9876)
        self.assertEqual(bootstrap.parse_args(["--", "--mcp-pump-seconds=0.5"])["pump_seconds"], 0.5)
        with self.assertRaises(ValueError):
            bootstrap.parse_args(["--", "--mcp-pump-seconds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
