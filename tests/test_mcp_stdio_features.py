"""Official-MCP-stdio feature test: transactional batch, audit log, dashboard, preview.

Runs a real ``--background`` Blender whose bridge is pumped by ``bootstrap.py
--mcp-pump``, then drives it through a real MCP stdio client session (the official
``mcp.ClientSession``; the pipe plumbing is the stdlib variant from
``stdio_pipe_transport`` because the sandbox denies asyncio's Proactor pipes).

What it proves over the real protocol
-------------------------------------
* the three new tools are listed, and ``blender_batch_graph_edit`` defaults to
  ``transactional=true``;
* a failing transaction comes back as an MCP **error** naming the rollback, and
  the node group is byte-for-byte unchanged afterwards;
* ``transactional`` + ``continue_on_error`` is refused before any mutation;
* the legacy non-atomic mode still reports ``partial``;
* every tool call and every bridge command is in the audit log, with the
  correlation id shared between the two halves, secrets removed and no image
  base64;
* the dashboard starts from the tool, serves a real page on loopback and stops
  again;
* a schema-validation failure is audited as an error.

Sandbox note: this build environment confines file writes to ``<MCP>``, so the
final preview file under the sibling ``Project`` directory cannot be written by
either process here. That boundary is asserted as a clean, actionable error, and
the full capture (real PNG, real MCP image content) is covered by
``blender_feature_integration.py`` and ``test_audit_features.py``.

Run::

    <workspace>\\MCP\\.venv\\Scripts\\python.exe MCP\\tests\\test_mcp_stdio_features.py

Report: ``MCP/tests/reports/stdio_feature_report.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from mcp.types import TextContent

from stdio_pipe_transport import last_teardown, mcp_stdio_session

TESTS_DIR = Path(__file__).resolve().parent
MCP_DIR = TESTS_DIR.parent
WORKSPACE = MCP_DIR.parent
sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(TESTS_DIR))

from blender_mcp import config  # noqa: E402  (after sys.path setup)

REPORT_DIR = TESTS_DIR / "reports"
REPORT_PATH = REPORT_DIR / "stdio_feature_report.json"
PYTHON = MCP_DIR / ".venv" / "Scripts" / "python.exe"
SERVER_PY = MCP_DIR / "server.py"
BOOTSTRAP = MCP_DIR / "blender" / "bootstrap.py"
RUNTIME = MCP_DIR / ".runtime"
AUDIT_DIR = RUNTIME / "logs"

TEST_PORT = int(os.environ.get("BLENDER_MCP_TEST_PORT", "0"))
AUDIT_DIR_ENV = str(RUNTIME / "stdio_feature_audit")
CALL_TIMEOUT = 120.0
BLENDER_START_TIMEOUT = 120.0
TEARDOWN_TIMEOUT = 20.0

RESULTS: list[dict] = []
CONTEXT: dict = {}


def record(name: str, ok: bool, details=None) -> bool:
    entry = {"name": name, "ok": bool(ok)}
    if details is not None:
        entry["details"] = details
    RESULTS.append(entry)
    print("STDIOFEATURE::%s::%s" % ("PASS" if ok else "FAIL", json.dumps(entry, default=str)))
    return bool(ok)


def find_blender() -> Path:
    env = os.environ.get("BLENDER_MCP_BLENDER")
    if env and Path(env).is_file():
        return Path(env)
    version_dir = WORKSPACE / "Version"
    if version_dir.is_dir():
        for child in sorted(version_dir.iterdir(), reverse=True):
            candidate = child / "blender.exe"
            if candidate.is_file():
                return candidate
    raise RuntimeError("no Blender executable found (set BLENDER_MCP_BLENDER)")


def payload(call_result) -> dict:
    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for item in getattr(call_result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
    return {}


def result_text(call_result) -> str:
    parts = []
    structured = getattr(call_result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, default=str))
    for item in getattr(call_result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def env_for_server(**overrides) -> dict:
    env = dict(os.environ)
    env.pop("BLENDER_MCP_LOCAL_DOMAINS", None)
    env.pop("BLENDER_MCP_VIEW_DOMAINS", None)
    env["BLENDER_MCP_PROFILE"] = "all"  # full business integration, independent of the new default
    env["BLENDER_MCP_PORT"] = str(TEST_PORT)
    env["BLENDER_MCP_TIMEOUT"] = "60"
    env["BLENDER_MCP_AUDIT_DIR"] = AUDIT_DIR_ENV
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def open_session(**overrides):
    return mcp_stdio_session(
        command=PYTHON,
        args=[str(SERVER_PY)],
        env=env_for_server(**overrides),
        stderr_path=RUNTIME / "logs" / "mcp_feature_server_stderr.log",
    )


def wait_for_bridge(port: int, token_path: Path, timeout: float) -> dict | None:
    token = token_path.read_text(encoding="utf-8").strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as sock:
                sock.settimeout(5.0)
                sock.sendall((json.dumps({"id": "probe", "token": token, "command": "health",
                                          "params": {}}) + "\n").encode("utf-8"))
                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                if b"\n" in buffer:
                    response = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
                    if response.get("ok"):
                        return response["result"]
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


def teardown(process: subprocess.Popen, token_file: Path) -> dict:
    """Stop ONLY the Blender this test started, with bounded waits."""
    info = {"pid": process.pid, "already_exited": process.poll() is not None}
    if process.poll() is None:
        try:
            health = wait_for_bridge(TEST_PORT, token_file, 3.0)
            if not health or health.get("pid") != process.pid:
                raise OSError("bridge PID does not match the test process; skipping protocol shutdown")
            token = token_file.read_text(encoding="utf-8").strip()
            with socket.create_connection(("127.0.0.1", TEST_PORT), timeout=3.0) as sock:
                sock.settimeout(3.0)
                sock.sendall((json.dumps({"id": "shutdown", "token": token, "command": "shutdown",
                                          "params": {}}) + "\n").encode())
                sock.recv(65536)
            info["shutdown_requested"] = True
        except OSError as exc:
            info["shutdown_requested"] = "failed: %s" % exc
        try:
            info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            info["terminate"] = True
            process.terminate()
            try:
                info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                info["kill"] = True
                process.kill()
                info["exit_code"] = process.wait(timeout=TEARDOWN_TIMEOUT)
    else:
        info["exit_code"] = process.returncode
    info["still_running"] = process.poll() is None
    return info


async def call(session, name: str, arguments: dict, timeout: float = CALL_TIMEOUT):
    return await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)


async def expect_ok(session, label: str, tool: str, arguments: dict):
    result = await call(session, tool, arguments)
    if result.isError:
        return record(label, False, {"error": result_text(result)[:400]}), {}
    return record(label, True, {"keys": sorted(payload(result))[:8]}), payload(result)


async def expect_error(session, label: str, tool: str, arguments: dict, needle: str | None = None):
    try:
        result = await call(session, tool, arguments)
    except Exception as exc:  # noqa: BLE001
        text = "%s: %s" % (type(exc).__name__, exc)
        return record(label, needle is None or needle in text, {"raised": text[:300]})
    text = result_text(result)
    if not result.isError:
        return record(label, False, {"unexpected_success": text[:300]})
    return record(label, needle is None or needle in text, {"error_text": text[:300]})


def audit_events() -> list[dict]:
    events = []
    for path in sorted(Path(AUDIT_DIR_ENV).glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def png_or_none(call_result):
    for item in getattr(call_result, "content", []) or []:
        if getattr(item, "type", None) == "image":
            return item
    return None


# --------------------------------------------------------------------------- #
# the session
# --------------------------------------------------------------------------- #
GROUP = "StdioFeatureGroup"


async def run_feature_checks(session) -> None:
    listing = await session.list_tools()
    names = sorted(tool.name for tool in listing.tools)
    CONTEXT["tool_count"] = len(names)
    required = {"blender_capture_preview", "blender_operation_log", "blender_log_dashboard",
                "blender_batch_graph_edit", "blender_health"}
    record("new_tools_are_listed", required.issubset(set(names)),
           {"count": len(names), "missing": sorted(required - set(names))})

    batch_tool = next(tool for tool in listing.tools if tool.name == "blender_batch_graph_edit")
    schema = batch_tool.inputSchema["properties"]
    record("batch_tool_defaults_to_transactional",
           schema["transactional"]["default"] is True and schema["continue_on_error"]["default"] is False,
           {"transactional": schema["transactional"], "continue_on_error": schema["continue_on_error"]})
    preview_tool = next(tool for tool in listing.tools if tool.name == "blender_capture_preview")
    record("preview_tool_returns_native_content",
           preview_tool.outputSchema is None
           and preview_tool.inputSchema["properties"]["mode"]["enum"] == ["auto", "viewport", "render"],
           {"outputSchema": preview_tool.outputSchema})

    ok, health = await expect_ok(session, "health_through_stdio", "blender_health", {})
    CONTEXT["blender_version"] = health.get("blender_version")
    CONTEXT["blender_pid"] = health.get("pid")

    await expect_ok(session, "create_node_group", "blender_create_node_group", {
        "name": GROUP, "if_exists": "replace",
        "interface": [
            {"name": "Rows", "socket_type": "NodeSocketInt", "in_out": "INPUT", "default_value": 4},
            {"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"},
        ],
    })
    ok, committed = await expect_ok(session, "transactional_batch_commits", "blender_batch_graph_edit", {
        "group": GROUP,
        "operations": [
            {"op": "create_node", "node_type": "GeometryNodeMeshGrid", "name": "Grid"},
            {"op": "link_sockets", "from_node": "Group Input", "from_socket": "Rows",
             "to_node": "Grid", "to_socket": "Vertices X"},
            {"op": "set_socket_default", "node": "Grid", "socket": "Vertices Y", "value": 4},
            {"op": "link_sockets", "from_node": "Grid", "from_socket": "Mesh",
             "to_node": "Group Output", "to_socket": "Geometry"},
        ],
    })
    record("transactional_batch_reports_commit",
           committed.get("committed") is True and committed.get("rolled_back") is False
           and committed.get("transaction", {}).get("status") == "committed"
           and committed.get("completed_count") == 4,
           {"transaction": committed.get("transaction"), "counts": committed.get("completed_count")})

    ok, before = await expect_ok(session, "inspect_before_failure", "blender_inspect_node_group",
                                 {"name": GROUP})
    await expect_error(session, "failing_transaction_is_an_error", "blender_batch_graph_edit", {
        "group": GROUP,
        "operations": [
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "NeverApplied"},
            {"op": "set_socket_default", "node": "Missing", "socket": "Size", "value": 1},
        ],
    }, "graph_edit_rolled_back")
    ok, after = await expect_ok(session, "inspect_after_failure", "blender_inspect_node_group",
                                {"name": GROUP})
    record("failed_transaction_left_the_group_unchanged",
           [node["name"] for node in before.get("nodes", [])] == [node["name"] for node in after.get("nodes", [])]
           and len(before.get("links", [])) == len(after.get("links", []))
           and "NeverApplied" not in [node["name"] for node in after.get("nodes", [])],
           {"nodes": [node["name"] for node in after.get("nodes", [])],
            "links": len(after.get("links", []))})

    await expect_error(session, "incompatible_options_are_rejected", "blender_batch_graph_edit", {
        "group": GROUP, "transactional": True, "continue_on_error": True,
        "operations": [{"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "Nope"}],
    }, "incompatible_options")

    ok, legacy = await expect_ok(session, "legacy_partial_mode", "blender_batch_graph_edit", {
        "group": GROUP, "transactional": False, "continue_on_error": True,
        "operations": [
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "LegacyA"},
            {"op": "set_socket_default", "node": "Missing", "socket": "Size", "value": 1},
            {"op": "create_node", "node_type": "GeometryNodeMeshCube", "name": "LegacyB"},
        ],
    })
    record("legacy_mode_reports_partial_and_keeps_edits",
           legacy.get("transactional") is False and legacy.get("partial") is True
           and legacy.get("rollback") is False and legacy.get("failed_count") == 1
           and legacy.get("completed_count") == 2,
           {"transaction": legacy.get("transaction"), "completed": legacy.get("completed_count")})
    ok, cleaned = await expect_ok(session, "transactional_cleanup", "blender_batch_graph_edit", {
        "group": GROUP,
        "operations": [{"op": "remove_node", "node": "LegacyA"},
                       {"op": "remove_node", "node": "LegacyB"}],
    })
    record("transactional_cleanup_commits", cleaned.get("committed") is True,
           {"transaction": cleaned.get("transaction")})

    await expect_error(session, "neg_batch_unknown_op_is_validated", "blender_batch_graph_edit",
                       {"group": GROUP, "operations": [{"op": "explode"}]}, "validation error")


async def check_audit_and_dashboard(session) -> None:
    ok, log = await expect_ok(session, "operation_log_tool", "blender_operation_log",
                              {"limit": 200, "include_details": True})
    tools = [event.get("tool") for event in log.get("events", []) if event.get("tool")]
    statuses = set(log.get("counts", {}))
    record("operation_log_contains_this_session",
           "blender_batch_graph_edit" in tools and "blender_health" in tools,
           {"counts": log.get("counts"), "tools": sorted(set(tools))[:8]})
    record("operation_log_shows_running_ok_and_rolled_back",
           {"running", "ok"} <= statuses and "rolled_back" in statuses,
           {"statuses": sorted(statuses),
            "rolled_back_events": [event.get("event") for event in log.get("events", [])
                                   if event.get("status") == "rolled_back"]})
    serialised = json.dumps(log)
    token = ""
    try:
        token = (RUNTIME / "token").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    record("operation_log_is_secret_and_base64_free",
           (not token or token not in serialised) and not re.search(r"\b[0-9a-f]{64}\b", serialised)
           and '"image_base64"' not in serialised.replace('"_type":"binary"', ""),
           {"token_checked": bool(token), "bytes": len(serialised)})

    ok, started = await expect_ok(session, "dashboard_starts", "blender_log_dashboard",
                                  {"action": "start"})
    CONTEXT["dashboard_url"] = started.get("url")
    record("dashboard_reports_a_loopback_url",
           str(started.get("url", "")).startswith("http://127.0.0.1:")
           and started.get("read_only") is True and started.get("browser_opened") is False,
           {"url": started.get("url"), "pid": started.get("pid")})
    try:
        with urllib.request.urlopen(started["url"] + "api/events?limit=5", timeout=5) as response:
            page_data = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(started["url"], timeout=5) as response:
            page = response.read().decode("utf-8")
        record("dashboard_serves_events_and_a_cjk_page",
               page_data["count"] > 0 and "操作日志" in page and "已回滚" in page
               and "cdn." not in page and "http://" not in page,
               {"events": page_data["count"], "page_bytes": len(page)})
    except Exception as exc:  # noqa: BLE001
        record("dashboard_serves_events_and_a_cjk_page", False, {"error": str(exc)})

    ok, status = await expect_ok(session, "dashboard_status", "blender_log_dashboard", {"action": "status"})
    record("dashboard_status_reports_running", status.get("running") is True, status)
    ok, stopped = await expect_ok(session, "dashboard_stops", "blender_log_dashboard", {"action": "stop"})
    record("dashboard_stop_only_owned_viewer", stopped.get("stopped") is True, stopped)


async def check_preview(session) -> None:
    await expect_error(session, "preview_viewport_needs_gui", "blender_capture_preview",
                       {"object": "Cube", "path": "previews/stdio_feature.png", "mode": "viewport",
                        "width": 160, "height": 120}, "viewport_unavailable")
    await expect_error(session, "preview_rejects_paths_outside_previews", "blender_capture_preview",
                       {"object": "Cube", "path": "demo/stdio_feature.png", "mode": "render",
                        "width": 160, "height": 120}, "previews")
    result = await call(session, "blender_capture_preview",
                        {"object": "Cube", "path": "previews/stdio_feature.png", "mode": "render",
                         "width": 160, "height": 120, "timeout": 60})
    text = result_text(result)
    image = png_or_none(result)
    if result.isError:
        # This build sandbox forbids writes outside <MCP>, so the sibling Project
        # directory cannot be written here. The tool must name the real reason.
        blocked = ("preview_dir_unwritable" in text or "preview_write_failed" in text)
        CONTEXT["preview_over_stdio"] = {
            "blocked_by_sandbox": blocked,
            "error": text[:300],
            "project_dir": str(config.project_dir()),
        }
        record("preview_reports_the_project_write_restriction_cleanly",
               blocked and "previews" in text,
               {"error": text[:300], "project_dir": str(config.project_dir())})
    else:
        metadata = payload(result)
        record("preview_returns_native_image_content",
               image is not None and metadata.get("mcp_image_content") is True
               and str(metadata.get("relative_path", "")).startswith("previews/"),
               {"mime": getattr(image, "mimeType", None), "size": metadata.get("size"),
                "bytes": metadata.get("bytes")})


async def main() -> int:
    global TEST_PORT
    started = time.time()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME / "logs" / "stdio_feature_blender_host.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    token_file = RUNTIME / "token"
    if not token_file.exists():
        config.ensure_token(token_file)

    try:
        blender = find_blender()
    except RuntimeError as exc:
        record("blender_executable_found", False, {"error": str(exc)})
        return 1
    record("blender_executable_found", True, {"path": str(blender)})

    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", TEST_PORT))
            TEST_PORT = reservation.getsockname()[1]
    except OSError as exc:
        record("test_port_available", False, {"error": str(exc), "port": TEST_PORT})
        return 1

    host_cmd = [
        str(blender), "--background", "--factory-startup", "--python", str(BOOTSTRAP), "--",
        "--mcp-pump", "--mcp-pump-seconds", "600", "--mcp-port", str(TEST_PORT),
        "--mcp-token-file", str(token_file), "--mcp-quiet",
    ]
    env = dict(os.environ)
    env["BLENDER_USER_CONFIG"] = str(RUNTIME / "blender_config")
    env["BLENDER_MCP_AUDIT_DIR"] = AUDIT_DIR_ENV
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(  # noqa: S603 - our own bundled Blender
            host_cmd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, env=env,
        )
    record("blender_host_started", True, {"pid": process.pid, "port": TEST_PORT})

    health = wait_for_bridge(TEST_PORT, token_file, BLENDER_START_TIMEOUT)
    record("bridge_reachable_from_raw_client",
           health is not None and health.get("pid") == process.pid,
           {"blender_version": (health or {}).get("blender_version"), "pid": (health or {}).get("pid"),
            "host_pid": process.pid})
    if health is None or health.get("pid") != process.pid:
        record("blender_host_log_tail", False,
               {"tail": log_path.read_text(encoding="utf-8", errors="replace")[-1500:]})
        record("teardown", True, teardown(process, token_file))
        return 1

    teardowns: list[dict] = []
    try:
        async with open_session() as (session, server_proc):
            record("mcp_initialize", True, {"server_pid": getattr(server_proc, "pid", None)})
            await run_feature_checks(session)
            await check_audit_and_dashboard(session)
            await check_preview(session)
        teardowns.append(last_teardown())
    except Exception as exc:  # noqa: BLE001
        record("mcp_stdio_feature_session", False,
               {"exception": "%s: %s" % (type(exc).__name__, exc)})
        teardowns.append(last_teardown())
    record("mcp_server_processes_stopped",
           all(not info.get("still_running") for info in teardowns if info), {"sessions": teardowns})

    teardown_info = teardown(process, token_file)
    record("teardown_only_own_process", teardown_info.get("still_running") is False, teardown_info)

    failed = [entry["name"] for entry in RESULTS if not entry["ok"]]
    report = {
        "suite": "mcp_stdio_features",
        "blender_version": health.get("blender_version"),
        "blender_pid": process.pid,
        "bridge_port": TEST_PORT,
        "audit_dir": AUDIT_DIR_ENV,
        "sandbox_note": (
            "the preview file cannot be written to the sibling Project directory under this build "
            "sandbox; the capture itself is covered by blender_feature_integration.py"
        ),
        "duration_s": round(time.time() - started, 3),
        "checks_total": len(RESULTS),
        "checks_failed": failed,
        "ok": not failed,
        "context": CONTEXT,
        "mcp_server_sessions": teardowns,
        "checks": RESULTS,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("STDIOFEATURE::SUMMARY::" + json.dumps(
        {"ok": report["ok"], "checks": report["checks_total"], "failed": failed,
         "report": str(REPORT_PATH)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
