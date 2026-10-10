"""Feature unit tests: audit log, live dashboard, transactional schema, preview.

Needs neither Blender nor a GPU.  Everything runs against real files under
``MCP/.runtime`` (never ``tempfile``: this build environment denies ``chmod`` in
the platform temp area, which makes ``TemporaryDirectory`` cleanup raise).

Covers what the feature contract asks for:

* every audited operation carries UTC timestamps, a session/request id, bounded
  and sanitised arguments, the outcome, the error and the duration;
* secret keys and the live token text never reach a log line;
* size rotation keeps archives and the reader keeps working across a rotation;
* several processes write different files and never interleave;
* a logging failure is reported on *stderr* and does not break the caller;
* the dashboard is loopback-only, read-only, bounded, has no CDN dependency,
  Chinese labels, and never serves an image's base64;
* the new MCP tools/schemas (transactional batch default, preview image
  content) behave as documented, including a schema-validation failure being
  audited.

Run::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_audit_features.py

Report: ``MCP/tests/reports/feature_unit_report.json``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import struct
import sys
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
REPORT_DIR = TESTS_DIR / "reports"
RUNTIME = MCP_DIR / ".runtime"
TMP_ROOT = RUNTIME / "test_tmp" / "feature_unit"

sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(TESTS_DIR))

os.environ.setdefault("BLENDER_MCP_AUDIT_DIR", str(TMP_ROOT / "default_audit"))

from blender_mcp import audit, config, log_viewer  # noqa: E402
from blender_mcp.bridge import BlenderCommandError, BridgeError  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fresh_dir(name: str) -> Path:
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def minimal_png(width: int = 32, height: int = 32) -> bytes:
    """A real, decodable PNG (two alternating colours, so it is provably not blank)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b""
    for y in range(height):
        rows += b"\x00"
        for x in range(width):
            rows += bytes((40, 90, 160) if (x + y) % 2 else (220, 200, 120))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class RecordingBridge:
    """A bridge stand-in that records the wire parameters of every call."""

    address = "127.0.0.1:0"

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.requests: list[dict] = []
        self.calls = 0

    def call(self, command, params=None, timeout=None):
        self.requests.append({"command": command, "params": params or {}, "timeout": timeout})
        self.calls += 1
        response = self.responses.get(command)
        if response is None:
            raise BlenderCommandError(command, "not_configured", "no canned response for %r" % command)
        if callable(response):
            return response(params or {})
        return response

    def close(self):  # pragma: no cover - interface compatibility
        pass


class NotYetStartedBridge:
    """Blender is not up for the first N health calls (the real startup workflow)."""

    address = "127.0.0.1:0"

    def __init__(self, failures: int = 2):
        self.remaining_failures = int(failures)
        self.requests: list[dict] = []
        self.health_calls = 0
        self.calls = 0

    def call(self, command, params=None, timeout=None):
        from blender_mcp.bridge import BridgeUnavailable

        self.requests.append({"command": command, "params": params or {}})
        self.calls += 1
        if command == "health":
            self.health_calls += 1
            if self.health_calls <= self.remaining_failures:
                raise BridgeUnavailable("cannot connect to the Blender bridge at 127.0.0.1:9876")
            return {"audit_protocol": audit.ADDON_AUDIT_PROTOCOL, "blender_version": "5.2.1 LTS"}
        if command == "ping":
            return {"pong": True, "protocol": 1}
        raise BlenderCommandError(command, "not_configured", "no canned response for %r" % command)

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# 1. sanitisation
# --------------------------------------------------------------------------- #
class AuditSanitizerTests(unittest.TestCase):
    def setUp(self):
        self.dir = fresh_dir("sanitize")
        self.token = "a" * 64
        self.log = audit.AuditLog(directory=self.dir, token=self.token, source="mcp")

    def entry(self, **fields) -> dict:
        self.log.record("probe", **fields)
        line = (self.dir / self.log.name).read_text(encoding="utf-8").strip().splitlines()[-1]
        return json.loads(line)

    def test_secret_keys_and_token_text_never_written(self):
        entry = self.entry(
            service_token=self.token,
            api_key="sk-live-should-not-appear",
            password="hunter2",
            authorization="Bearer %s" % self.token,
            free_text="the token is %s and that is all" % self.token,
            header="token=%s" % self.token,
            token_present=True,
            token_path="C:/somewhere/token",
        )
        self.assertEqual(entry["service_token"], "***")
        self.assertEqual(entry["api_key"], "***")
        self.assertEqual(entry["password"], "***")
        self.assertEqual(entry["authorization"], "***")
        self.assertNotIn(self.token, json.dumps(entry))
        self.assertIn("***", entry["free_text"])
        self.assertTrue(entry["token_present"], "a boolean *_present flag is not a secret")
        self.assertIn("token", entry["token_path"])

    def test_large_values_are_bounded_and_hashed(self):
        values = [[float(i), 0.0, 1.0] for i in range(5000)]
        entry = self.entry(big=values, text="x" * 4000,
                           nested={"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}})
        self.assertEqual(entry["big"]["length"], 5000)
        self.assertEqual(entry["big"]["sample_size"], audit.MAX_ITEMS)
        self.assertTrue(entry["big"]["sha256"], "a large array must carry a digest")
        self.assertEqual(len(entry["big"]["sample"]), audit.MAX_ITEMS)
        self.assertLess(len(entry["text"]), 400)
        self.assertIn("truncated", json.dumps(entry["nested"]),
                      "nesting deeper than MAX_DEPTH must be truncated, not stored")

    def test_binary_payloads_are_summarized(self):
        entry = self.entry(image_base64="A" * 5000, raw_bytes=b"\x00" * 100)
        self.assertEqual(entry["image_base64"]["_type"], "binary")
        self.assertEqual(entry["image_base64"]["length"], 5000)
        self.assertEqual(entry["raw_bytes"]["length"], 100)

    def test_limits_are_documented(self):
        described = audit.describe_directory(self.dir)
        self.assertEqual(described["limits"]["max_items"], audit.MAX_ITEMS)
        self.assertIn("max_string", described["limits"])


# --------------------------------------------------------------------------- #
# 2. rotation / reading back
# --------------------------------------------------------------------------- #
class AuditRotationTests(unittest.TestCase):
    def test_rotation_keeps_archives_and_reading_survives(self):
        directory = fresh_dir("rotation")
        log = audit.AuditLog(directory=directory, source="mcp", max_bytes=4096, keep_archives=3)
        for index in range(400):
            log.record("tool_end", tool="t%d" % index, status="ok", payload="y" * 120)
        archives = sorted(directory.glob("%s.*.jsonl" % log.path.stem))
        self.assertTrue(archives, "rotation must produce archives")
        self.assertLessEqual(len(archives), 3, "keep_archives must be honoured")
        self.assertTrue(log.path.exists(), "the live file must continue after a rotation")
        tail = audit.tail_events(limit=500, directory=directory)
        self.assertTrue(tail, "events must remain readable after a rotation")
        self.assertLess(len(tail), 400, "pruned archives must not be resurrected by the reader")
        self.assertEqual(tail[-1]["tool"], "t399", "the newest event must survive rotation")
        self.assertTrue(all("ts" in event and "seq" in event for event in tail))
        self.assertTrue(all(isinstance(event["seq"], int) for event in tail))
        self.assertEqual(tail[-1]["seq"], 400)

    def test_tail_filters(self):
        directory = fresh_dir("filters")
        log = audit.AuditLog(directory=directory, source="mcp")
        log.record("tool_end", tool="blender_health", status="ok", duration_ms=1.0)
        log.record("tool_end", tool="blender_batch_graph_edit", status="error", message="boom")
        log.record("batch_rollback", tool="blender_batch_graph_edit", status="rolled_back", reason="rolled_back")
        self.assertEqual(len(audit.tail_events(directory=directory)), 3)
        self.assertEqual(len(audit.tail_events(status="error", directory=directory)), 1)
        self.assertEqual(len(audit.tail_events(status="rolled_back", directory=directory)), 1)
        self.assertEqual(len(audit.tail_events(query="batch", directory=directory)), 2)
        self.assertEqual(len(audit.tail_events(limit=500, directory=directory)), 3)
        self.assertEqual(audit.tail_events(directory=directory)[-1]["status"], "rolled_back")
        counts = audit.status_counts(audit.tail_events(directory=directory))
        self.assertEqual(counts, {"ok": 1, "error": 1, "rolled_back": 1})

    def test_start_and_end_events_share_a_request_id(self):
        directory = fresh_dir("correlation")
        log = audit.AuditLog(directory=directory, source="mcp")
        with audit.request_scope("abc123", tool="blender_health"):
            self.assertEqual(audit.current_request_id(), "abc123")
            log.record("tool_start", request_id=audit.current_request_id(), status="running")
            log.record("tool_end", request_id=audit.current_request_id(), status="ok", duration_ms=2.5)
        self.assertIsNone(audit.current_request_id())
        events = audit.tail_events(directory=directory)
        self.assertEqual({event["request_id"] for event in events}, {"abc123"})
        self.assertEqual([audit._event_status(e) for e in events], ["running", "ok"])

    def test_two_processes_never_share_a_file(self):
        directory = fresh_dir("processes")
        first = audit.AuditLog(directory=directory, source="mcp", session="aaaa")
        second = audit.AuditLog(directory=directory, source="bridge", session="bbbb")
        self.assertNotEqual(first.name, second.name)
        first.record("tool_end", status="ok")
        second.record("command_end", status="ok")
        self.assertEqual(len(audit.tail_events(source="mcp", directory=directory)), 1)
        self.assertEqual(len(audit.tail_events(source="bridge", directory=directory)), 1)
        self.assertEqual(len(audit.tail_events(directory=directory)), 2)

    def test_pending_counts_only_outstanding_work_in_live_sessions(self):
        """已完成 vs 进行中: a finished (or stopped) session must never look pending."""
        directory = fresh_dir("pending")
        log = audit.AuditLog(directory=directory, source="mcp", session="live")
        log.record("session_start")
        log.record("tool_start", request_id="req-1", tool="blender_health", status="running")
        log.record("tool_end", request_id="req-1", tool="blender_health", status="ok", ok=True)
        log.record("tool_start", request_id="req-2", tool="blender_health", status="running")
        # A second, already stopped session: its open start is history, not pending.
        stopped = audit.AuditLog(directory=directory, source="mcp", session="stopped")
        stopped.record("session_start")
        stopped.record("tool_start", request_id="req-3", tool="blender_object_info", status="running")
        stopped.record("session_stop")
        # And one genuinely outstanding bridge command in a live session.
        bridge = audit.AuditLog(directory=directory, source="bridge", session="live-bridge")
        bridge.record("command_start", request_id="req-4", command="health", status="running")

        events = audit.tail_events(limit=200, directory=directory)
        annotated, summary = audit.annotate_operations(events)
        self.assertEqual(summary["running"], 2,
                         "only the live session's open calls may count as pending")
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["operations"],
                         summary["ok"] + summary["error"] + summary["running"] + summary["rolled_back"])
        self.assertIn("operations", summary["note"])
        by_request = {entry.get("request_id"): entry for entry in annotated
                      if entry.get("event") in audit.START_KINDS}
        self.assertEqual(by_request["req-1"]["display_status"], "completed")
        self.assertEqual(by_request["req-2"]["display_status"], "running")
        self.assertEqual(by_request["req-3"]["display_status"], "info",
                         "an open start from a stopped session is historical info")
        self.assertEqual(by_request["req-1"]["paired_with_seq"],
                         by_request["req-1"]["seq"] + 1)

    def test_viewer_reports_operation_counts_not_historical_event_counts(self):
        directory = fresh_dir("viewer_pending")
        log = audit.AuditLog(directory=directory, source="mcp", session="live")
        for index in range(3):
            log.record("tool_start", request_id="done-%d" % index, tool="blender_health",
                       status="running")
            log.record("tool_end", request_id="done-%d" % index, tool="blender_health", status="ok",
                       ok=True)
        log.record("session_start")
        viewer = log_viewer.LogViewer(directory=directory, owner="unit-test", publish=False).start()
        try:
            payload = viewer.events_payload({"limit": ["50"]})
        finally:
            viewer.stop()
        counts = payload["counts"]
        self.assertEqual(counts["running"], 0,
                         "three finished calls must not be reported as 进行中 3")
        self.assertEqual(counts["operations"], 3)
        self.assertEqual(counts["ok"], 3)
        self.assertEqual(counts["events"], 7)
        self.assertIn("operations", counts["note"])
        self.assertEqual(payload["window"], log_viewer.OPERATION_WINDOW)

    def test_write_failure_is_reported_but_never_raised(self):
        blocked = fresh_dir("blocked_parent") / "blocked"
        if blocked.exists():
            blocked.unlink()
        blocked.write_text("this is a file, not a directory", encoding="utf-8")
        log = audit.AuditLog(directory=blocked, source="mcp")
        import contextlib
        import io

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            entry = log.record("tool_end", status="ok")
        self.assertIsNone(entry, "an unwritable log must not fabricate an entry")
        self.assertGreater(log.failures, 0)
        self.assertIn("audit", captured.getvalue())
        self.assertLess(len(captured.getvalue()), 4096, "the warning must stay bounded")


# --------------------------------------------------------------------------- #
# 3. dashboard
# --------------------------------------------------------------------------- #
class LogViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = fresh_dir("viewer")
        cls.log = audit.AuditLog(directory=cls.directory, source="mcp", token="f" * 64)
        cls.log.record("tool_start", request_id="req-1", tool="blender_health", status="running",
                       args={"object": "Cube"})
        cls.log.record("tool_end", request_id="req-1", tool="blender_health", status="ok", duration_ms=3.5,
                       result={"blender_version": "5.2.1 LTS", "image_base64": "Q" * 2048})
        cls.log.record("batch_rollback", request_id="req-2", tool="blender_batch_graph_edit",
                       status="rolled_back", reason="rolled_back",
                       steps=[{"index": 0, "op": "create_node", "ok": True},
                              {"index": 3, "op": "link_sockets", "ok": False, "code": "socket_not_found",
                               "error": "no socket"}])
        cls.viewer = log_viewer.LogViewer(directory=cls.directory, owner="unit-test").start()
        cls.url = cls.viewer.url

    @classmethod
    def tearDownClass(cls):
        cls.viewer.stop()

    def fetch(self, path: str, method: str = "GET"):
        request = urllib.request.Request(self.url.rstrip("/") + path, method=method)
        return urllib.request.urlopen(request, timeout=5)

    def test_events_endpoint_is_bounded_and_filtered(self):
        with self.fetch("/api/events?limit=2") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2, "limit must be honoured")
        self.assertEqual(payload["limit"], 2)
        with self.fetch("/api/events?limit=99999") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["limit"], log_viewer.MAX_LIMIT)
        with self.fetch("/api/events?status=rolled_back") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual([event["event"] for event in payload["events"]], ["batch_rollback"])
        self.assertIn("rolled_back", payload["counts"])
        self.assertIn("limits", payload)

    def test_viewer_never_serves_image_base64(self):
        with self.fetch("/api/events?limit=50") as response:
            raw = response.read().decode("utf-8")
        self.assertNotIn("Q" * 64, raw, "base64 image data must never reach the browser")
        self.assertIn("binary", raw)
        with self.fetch("/") as response:
            page = response.read().decode("utf-8")
        self.assertNotIn("Q" * 64, page)

    def test_dashboard_page_is_cjk_and_dependency_free(self):
        with self.fetch("/") as response:
            page = response.read().decode("utf-8")
        for label in ("操作日志", "成功", "错误", "进行中", "已回滚", "自动滚动", "暂停刷新",
                      "已完成", "信息", "操作合计", "项"):
            self.assertIn(label, page)
        for remote in ("http://", "https://", "cdn.", "unpkg", "jsdelivr"):
            self.assertNotIn(remote, page, "the dashboard must not depend on any CDN")
        self.assertIn("/api/events", page)

    def test_dashboard_preserves_rows_scroll_and_expansion(self):
        """The 1.5 s refresh must not wipe the list, collapse details or jump scroll."""
        with self.fetch("/") as response:
            page = response.read().decode("utf-8")
        self.assertNotIn('list.textContent = ""',
                         page, "re-rendering the whole list destroys expanded rows and scroll")
        self.assertIn("state.nodes", page)
        self.assertIn("rowKey", page)
        self.assertIn("entry.signature === signature", page,
                      "unchanged rows must not be rewritten")
        self.assertIn("classList.contains(\"open\")", page, "the open state must survive a refresh")
        self.assertIn("if (follow) { window.scrollTo", page,
                      "scroll must only be forced when follow is checked")

    def test_read_only_and_bounded_surface(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/api/events", method="POST")
        self.assertEqual(ctx.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/etc/passwd")
        self.assertEqual(ctx.exception.code, 404)
        # A path-looking parameter is only ever a query value, never a file read.
        with self.fetch("/api/events?file=../../token") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"], "a path-like parameter must not be interpreted as a path")
        self.assertEqual(len(payload["events"]), 3)

    def test_health_and_metadata_and_reuse(self):
        with self.fetch("/api/health") as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertEqual(health["service"], log_viewer.SERVICE)
        self.assertEqual(health["pid"], os.getpid())
        self.assertTrue(health["read_only"])
        self.assertTrue(health["url"].startswith("http://127.0.0.1:"))
        metadata = self.directory / ("viewer-%d.json" % os.getpid())
        self.assertTrue(metadata.exists(), "per-process metadata must be published atomically")
        live = log_viewer.find_live_viewer(self.directory)
        self.assertIsNotNone(live)
        self.assertTrue(live["owned_by_this_process"])
        reused = log_viewer.start_viewer(directory=self.directory, reuse_live=True)
        self.assertFalse(reused["started"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["url"], self.url)
        with urllib.request.urlopen(self.url.rstrip("/") + "/api/stream", timeout=6) as response:
            chunk = response.read(120).decode("utf-8")
        self.assertTrue(chunk.startswith("data: "), "the SSE endpoint must emit data frames")

    def test_port_conflict_is_explicit(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            with self.assertRaises(OSError) as ctx:
                log_viewer.LogViewer(directory=self.directory, port=port, publish=False).start()
            self.assertIn("log dashboard", str(ctx.exception))
        finally:
            holder.close()

    def test_non_loopback_host_is_refused(self):
        with self.assertRaises(ValueError):
            log_viewer.LogViewer(directory=self.directory, host="0.0.0.0", publish=False)


# --------------------------------------------------------------------------- #
# 4. MCP tool layer: schemas, audit records, image content
# --------------------------------------------------------------------------- #
class McpFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from blender_mcp.tools import build_server

        cls.bridge = RecordingBridge({"health": {"audit_protocol": audit.ADDON_AUDIT_PROTOCOL,
                                                 "blender_version": "5.2.1 LTS"}})
        cls.mcp = build_server(cls.bridge, profile="all")
        cls.tools = {tool.name: tool for tool in asyncio.run(cls.mcp.list_tools())}

    def setUp(self):
        self.bridge.responses.pop("batch_graph_edit", None)
        self.bridge.responses.pop("capture_preview", None)
        self.directory = fresh_dir("mcp_audit_%s" % self._testMethodName)
        audit.configure(directory=self.directory, source="mcp", token="b" * 64)

    def call(self, name, arguments):
        return asyncio.run(self.mcp.call_tool(name, arguments))

    @staticmethod
    def text_of(result) -> str:
        from mcp.types import TextContent

        content = result if isinstance(result, list) else result.content
        return "\n".join(item.text for item in content if isinstance(item, TextContent))

    def events(self, **filters):
        return audit.tail_events(directory=self.directory, limit=500, **filters)

    def test_new_tools_and_schemas(self):
        for name in ("blender_capture_preview", "blender_operation_log", "blender_log_dashboard"):
            self.assertIn(name, self.tools)
        batch = self.tools["blender_batch_graph_edit"].inputSchema["properties"]
        self.assertEqual(batch["transactional"]["default"], True,
                         "blender_batch_graph_edit must be transactional by default")
        self.assertEqual(batch["continue_on_error"]["default"], False)
        preview = self.tools["blender_capture_preview"]
        self.assertIsNone(preview.outputSchema,
                          "the preview tool returns native content, not a structured schema")
        self.assertEqual(preview.inputSchema["properties"]["mode"]["enum"], ["auto", "viewport", "render"])
        self.assertEqual(preview.inputSchema["properties"]["view"]["enum"],
                         ["current", "isometric", "front", "top", "back", "right"])
        self.assertEqual(preview.inputSchema["properties"]["width"]["default"], 1280)
        dash = self.tools["blender_log_dashboard"].inputSchema["properties"]["action"]
        self.assertEqual(dash["enum"], ["start", "status", "stop"])

    def test_tool_success_is_audited_with_args_result_and_duration(self):
        self.call("blender_health", {})
        events = self.events()
        kinds = [event["event"] for event in events]
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_end", kinds)
        start = next(event for event in events if event["event"] == "tool_start")
        end = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(start["tool"], "blender_health")
        self.assertEqual(start["status"], "running")
        self.assertIsInstance(start["args"], dict)
        self.assertEqual(start["request_id"], end["request_id"])
        self.assertEqual(end["status"], "ok")
        self.assertGreaterEqual(end["duration_ms"], 0.0)
        self.assertIn("result", end)
        health = end["result"]["result"]
        self.assertEqual(health["blender_version"], "5.2.1 LTS")
        self.assertGreaterEqual(health["mcp_server"]["calls_made"], 1)
        self.assertIn("mcp_server", health)

    def test_bridge_call_is_audited_and_correlated(self):
        self.call("blender_health", {})
        events = self.events()
        bridge_events = [event for event in events if event["event"].startswith("bridge_")]
        self.assertTrue(bridge_events, "the bridge wrapper must audit every command")
        tool_end = next(event for event in events if event["event"] == "tool_end")
        correlated = [event for event in bridge_events if event["request_id"] == tool_end["request_id"]]
        self.assertTrue(correlated, "the bridge event must carry the tool call's request id")
        self.assertIn("health", [event["command"] for event in bridge_events])
        wire = [request for request in self.bridge.requests if request["command"] == "health"]
        self.assertTrue(any(audit.RESERVED_PARAM in request["params"] for request in wire),
                        "the reserved correlation field must reach the wire once the add-on supports it")

    def test_schema_validation_failure_is_audited(self):
        with self.assertRaises(Exception):
            self.call("blender_object_info", {"wrong": 1})
        events = self.events()
        end = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(end["status"], "error")
        self.assertFalse(end["ok"])
        self.assertEqual(end["tool"], "blender_object_info")
        self.assertIn("name", end["message"])
        self.assertIn("error_type", end)

    def test_blender_error_is_audited_as_error(self):
        with self.assertRaises(Exception):
            self.call("blender_batch_graph_edit", {"group": "G", "operations": [{"op": "create_node"}]})
        end = next(event for event in self.events() if event["event"] == "tool_end")
        self.assertEqual(end["status"], "error")
        self.assertEqual(end["error_type"], "ToolError")
        self.assertIn("not_configured", end["message"])

    def test_correlation_is_regained_after_an_initial_failure(self):
        """health-before-launch must not lose bridge correlation forever."""
        from blender_mcp.tools import build_server

        self.directory = fresh_dir("recovery_%s" % self._testMethodName)
        audit.configure(directory=self.directory, source="mcp", token="d" * 64)
        bridge = NotYetStartedBridge(failures=2)   # probe + the real call both fail
        server = build_server(bridge, profile="all")

        with self.assertRaises(Exception):
            asyncio.run(server.call_tool("blender_health", {}))
        events = audit.tail_events(limit=200, directory=self.directory)
        unavailable = [event for event in events if event["event"] == "bridge_audit_unavailable"]
        self.assertTrue(unavailable, "the failed probe must be recorded")
        self.assertEqual(unavailable[0]["correlation"], "unavailable")
        self.assertGreaterEqual(unavailable[0]["probe_attempts"], 1)

        asyncio.run(server.call_tool("blender_health", {}))   # Blender is up now
        events = audit.tail_events(limit=200, directory=self.directory)
        regained = [event for event in events if event["event"] == "bridge_audit_regained"]
        self.assertTrue(regained, "correlation must be regained without any replay")
        self.assertEqual(regained[0]["correlation"], "restored")
        self.assertGreaterEqual(regained[0]["failed_probes"], 1)

        asyncio.run(server.call_tool("blender_ping", {}))
        pings = [request for request in bridge.requests if request["command"] == "ping"]
        self.assertTrue(pings)
        self.assertIn(audit.RESERVED_PARAM, pings[-1]["params"],
                      "after recovery the correlation field must reach the wire again")
        linked = [event for event in audit.tail_events(limit=200, directory=self.directory)
                  if event["event"] == "bridge_ok" and event.get("correlation") == "linked"]
        self.assertTrue(linked, "recovered bridge calls are audited as correlated")

    def test_legacy_partial_batch_is_audited_with_a_truthful_ok_flag(self):
        """A non-transactional batch that failed an operation is not a success."""
        def legacy_response(_params):
            return {
                "group": "G",
                "transactional": False,
                "committed": False,
                "rolled_back": False,
                "rollback": False,
                "partial": True,
                "completed_count": 1,
                "failed_count": 1,
                "completed": [{"index": 0, "op": "create_node", "ok": True}],
                "failures": [{"index": 1, "op": "set_socket_default", "ok": False,
                              "code": "node_not_found", "error": "no node"}],
                "transaction": {"mode": "in_place", "status": "partial"},
                "note": "legacy",
            }

        self.bridge.responses["batch_graph_edit"] = legacy_response
        result = self.call("blender_batch_graph_edit",
                           {"group": "G", "transactional": False, "continue_on_error": True,
                            "operations": [{"op": "create_node", "node_type": "X"}]})
        payload_result = json.loads(self.text_of(result))
        self.assertFalse(payload_result["rolled_back"], "legacy partial mode is not rolled back")
        self.assertTrue(payload_result["partial"])
        end = next(event for event in self.events() if event["event"] == "tool_end")
        self.assertEqual(end["status"], "error")
        self.assertFalse(end["ok"], "a partial legacy batch must not be audited as ok")
        self.assertEqual(end["committed"], False)
        self.assertIsInstance(end["duration_ms"], float)
        steps = [event for event in self.events() if event["event"] == "batch_step"]
        self.assertEqual([step["ok"] for step in steps], [True, False])

    def test_batch_steps_and_rollback_are_audited(self):
        def batch_response(_params):
            return {
                "group": "G",
                "transactional": True,
                "committed": False,
                "rolled_back": True,
                "rollback": True,
                "partial": False,
                "completed_count": 2,
                "failed_count": 1,
                "completed": [{"index": 0, "op": "create_node", "ok": True},
                              {"index": 1, "op": "link_sockets", "ok": True}],
                "failures": [{"index": 2, "op": "remove_node", "ok": False,
                              "code": "node_not_found", "error": "no node"}],
                "transaction": {"mode": "isolated_staging", "status": "rolled_back"},
                "note": "rolled back",
            }

        self.bridge.responses["batch_graph_edit"] = batch_response
        with self.assertRaises(Exception) as ctx:
            self.call("blender_batch_graph_edit", {"group": "G",
                                                   "operations": [{"op": "create_node", "node_type": "X"}]})
        self.assertIn("graph_edit_rolled_back", str(ctx.exception))
        events = self.events()
        steps = [event for event in events if event["event"] == "batch_step"]
        self.assertEqual(len(steps), 3)
        self.assertEqual([step["ok"] for step in steps], [True, True, False])
        self.assertEqual(steps[-1]["code"], "node_not_found")
        rollback = next(event for event in events if event["event"] == "batch_rollback")
        self.assertEqual(rollback["status"], "rolled_back")
        end = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(end["status"], "rolled_back")
        self.assertFalse(end["committed"])

    def test_preview_returns_native_image_content_and_audits_without_base64(self):
        project = fresh_dir("preview_project")
        png = minimal_png(32, 32)
        captured = {}

        def capture(params):
            target = (project / params["relative_path"]).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            captured["target"] = target
            return {
                "path": str(target),
                "relative_path": params["relative_path"],
                "mode": "render",
                "requested_mode": params["mode"],
                "view": params["view"],
                "width": 32,
                "height": 32,
                "size": {"width": 32, "height": 32},
                "bytes": len(png),
                "image_base64": base64.b64encode(png).decode("ascii"),
                "evaluated": {"vertices": 16, "bounds": {"min": [0, 0, 0], "max": [1, 1, 0], "size": [1, 1, 0]}},
                "diagnostics": {"engine": "BLENDER_WORKBENCH", "restored": True},
            }

        self.bridge.responses["capture_preview"] = capture
        original = config.PROJECT_DIR
        config.PROJECT_DIR = project
        try:
            result = self.call("blender_capture_preview",
                               {"object": "Cube", "path": "previews/unit.png", "width": 32, "height": 32})
        finally:
            config.PROJECT_DIR = original

        from mcp.types import ImageContent, TextContent

        self.assertFalse(result.isError)
        image = next(item for item in result.content if isinstance(item, ImageContent))
        text = next(item for item in result.content if isinstance(item, TextContent))
        self.assertEqual(image.mimeType, "image/png")
        self.assertEqual(base64.b64decode(image.data), png)
        metadata = json.loads(text.text)
        self.assertEqual(metadata["path"], str(captured["target"]))
        self.assertEqual(metadata["relative_path"], "previews/unit.png")
        self.assertEqual(metadata["mode"], "render")
        self.assertEqual(metadata["bytes"], len(png))
        self.assertTrue(metadata["mcp_image_content"])
        self.assertEqual(metadata["size"], {"width": 32, "height": 32})
        self.assertNotIn("image_base64", metadata)

        events = self.events()
        end = next(event for event in events if event["event"] == "tool_end")
        self.assertEqual(end["status"], "ok")
        self.assertEqual(end["result"]["content_types"], ["image", "text"])
        self.assertEqual(end["result"]["image_bytes"], len(png))
        self.assertNotIn(base64.b64encode(png).decode("ascii")[:64],
                         json.dumps(self.events()))

    def test_preview_rejects_paths_outside_previews(self):
        original = config.PROJECT_DIR
        project = fresh_dir("preview_project_reject")
        config.PROJECT_DIR = project
        try:
            for bad in ("demo/x.png", "previews/../../escape.png", "C:/tmp/x.png",
                        "previews/x.jpg", "x.png", "previews/", "..\\previews\\x.png"):
                with self.assertRaises(Exception) as ctx:
                    self.call("blender_capture_preview", {"path": bad, "width": 32, "height": 32})
                self.assertIn("ToolError", type(ctx.exception).__name__,
                              "path %r must be refused with a tool error" % bad)
            for width, height in ((8, 32), (4096, 32), (32, 4096)):
                with self.assertRaises(Exception):
                    self.call("blender_capture_preview",
                              {"path": "previews/x.png", "width": width, "height": height})
        finally:
            config.PROJECT_DIR = original

    def test_dashboard_and_log_tools_work_through_the_tool_layer(self):
        self.call("blender_health", {})
        payload = json.loads(self.text_of(self.call("blender_operation_log", {"limit": 10})))
        self.assertGreaterEqual(payload["count"], 2)
        self.assertIn("mcp", [event["source"] for event in payload["events"]])
        self.assertTrue(payload["directory"].endswith("mcp_audit_%s" % self._testMethodName))
        started = json.loads(self.text_of(self.call("blender_log_dashboard", {"action": "start"})))
        self.assertTrue(started["running"])
        self.assertTrue(started["url"].startswith("http://127.0.0.1:"))
        self.assertFalse(started["browser_opened"])
        with urllib.request.urlopen(started["url"] + "api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertEqual(health["service"], log_viewer.SERVICE)
        status = json.loads(self.text_of(self.call("blender_log_dashboard", {"action": "status"})))
        self.assertTrue(status["running"])
        stopped = json.loads(self.text_of(self.call("blender_log_dashboard", {"action": "stop"})))
        self.assertTrue(stopped["stopped"])
        again = json.loads(self.text_of(self.call("blender_log_dashboard", {"action": "status"})))
        self.assertFalse(again["running"])


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    started = time.time()
    result = runner.run(suite)
    payload = {
        "suite": "feature_unit",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "duration_s": round(time.time() - started, 3),
        "tests_run": result.testsRun,
        "failures": [str(test) for test, _ in result.failures],
        "errors": [str(test) for test, _ in result.errors],
        "skipped": [str(test) for test, _ in result.skipped],
        "ok": result.wasSuccessful(),
    }
    (REPORT_DIR / "feature_unit_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
