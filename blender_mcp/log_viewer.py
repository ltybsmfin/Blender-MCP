"""Local, read-only live dashboard for the MCP audit log.

Design
------
* **Loopback only.** The socket binds ``127.0.0.1``; every request is additionally
  checked to come from a loopback peer, and only ``GET``/``HEAD`` are answered.
* **No file access over HTTP.** The only files the server reads are
  ``audit-*.jsonl`` inside the one directory it was constructed with. No query
  parameter names a path, and no endpoint writes anything.
* **No tokens.** Events are sanitised when they are written (see
  :mod:`blender_mcp.audit`) and sanitised again when they are served, so a token
  can never reach the browser. Image payloads are shown as a size + hash, never
  as base64.
* **Standard library only.** ``http.server`` serves one small inline HTML page
  (no CDN, no build step) plus a JSON polling endpoint and a bounded SSE stream.
* **Race-safe startup.** The port defaults to ``0`` (the OS assigns a free one)
  and the chosen URL is published to ``<log dir>/viewer-<pid>.json`` with an
  atomic rename, so several viewers - or several MCP servers - can never claim
  the same address. A requested port that is already taken is an explicit error.

Run standalone (this is what ``MCP/launch_log_viewer.ps1`` does)::

    python -m blender_mcp.log_viewer --root MCP/.runtime/logs --port 0 --open
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit

SERVICE = "blender-mcp-log-viewer"
VIEWER_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
#: How many events are read to pair starts with ends before the display slice is cut.
OPERATION_WINDOW = 1500
SSE_MAX_SECONDS = 90.0
SSE_INTERVAL = 1.5

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blender MCP 操作日志</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
        background: #14161a; color: #e6e8eb; font-size: 13px; }
 header { position: sticky; top: 0; z-index: 5; background: #1b1f26; border-bottom: 1px solid #2c323c;
          padding: 10px 14px; }
 h1 { font-size: 15px; margin: 0 0 6px 0; font-weight: 600; }
 .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
 .pill { border-radius: 10px; padding: 2px 8px; font-size: 12px; border: 1px solid #39404c; background: #232833; }
 .pill.ok { border-color: #2f7d4f; color: #7ee2a8; }
 .pill.error { border-color: #8d3535; color: #ff9b9b; }
 .pill.running { border-color: #8a6d1f; color: #ffd479; }
 .pill.rolled_back { border-color: #6a4a8d; color: #d0a9ff; }
 .pill.info { color: #9aa4b2; }
 .muted { color: #9aa4b2; }
 input, select, button { background: #232833; color: #e6e8eb; border: 1px solid #39404c;
                         border-radius: 6px; padding: 4px 8px; font-size: 12px; }
 button { cursor: pointer; }
 #list { padding: 8px 14px 40px 14px; }
 .ev { border: 1px solid #262c36; border-left: 3px solid #39404c; border-radius: 6px; margin: 6px 0;
       background: #181c22; }
 .ev.ok { border-left-color: #2f7d4f; }
 .ev.error { border-left-color: #8d3535; }
 .ev.running { border-left-color: #8a6d1f; }
 .ev.rolled_back { border-left-color: #6a4a8d; }
 .head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; padding: 6px 10px; cursor: pointer; }
 .ts { color: #8b95a3; font-variant-numeric: tabular-nums; min-width: 168px; }
 .src { color: #7fb0ff; min-width: 62px; }
 .evname { color: #cdd6e2; min-width: 132px; }
 .tool { color: #ffd479; }
 .dur { color: #9aa4b2; min-width: 62px; text-align: right; }
 .det { display: none; padding: 0 10px 10px 10px; }
 .ev.open .det { display: block; }
 pre { margin: 4px 0 0 0; padding: 8px; background: #10131a; border: 1px solid #242a34; border-radius: 6px;
       overflow-x: auto; max-height: 320px; white-space: pre-wrap; word-break: break-word; }
 .steps { margin: 6px 0 0 0; padding-left: 16px; }
 .steps li { margin: 2px 0; }
 .err { color: #ff9b9b; }
 footer { padding: 8px 14px; color: #77808d; }
</style>
</head>
<body>
<header>
  <h1>Blender MCP 操作日志（实时）</h1>
  <div class="row">
    <span class="pill" id="p-ok">成功 0 项</span>
    <span class="pill error" id="p-error">错误 0 项</span>
    <span class="pill running" id="p-running">进行中 0 项</span>
    <span class="pill rolled_back" id="p-rolled">已回滚 0 项</span>
    <span class="muted" id="p-opnote">（数字为“操作”计数：一次 MCP 调用与其桥命令各算一项）</span>
  </div>
  <div class="row" style="margin-top:4px">
    <span class="muted" id="p-meta">等待数据…</span>
  </div>
  <div class="row" style="margin-top:8px">
    <select id="f-status" title="按状态筛选">
      <option value="">全部状态</option>
      <option value="ok">成功 ok</option>
      <option value="error">错误 error</option>
      <option value="running">进行中 running</option>
      <option value="rolled_back">已回滚 rolled_back</option>
      <option value="completed">已完成 completed</option>
      <option value="info">信息 info</option>
    </select>
    <select id="f-source" title="按来源筛选">
      <option value="">全部来源</option>
      <option value="mcp">MCP 服务端 mcp</option>
      <option value="bridge">Blender 桥接 bridge</option>
    </select>
    <input id="f-query" placeholder="关键词：工具名 / 错误 / 命令" size="28">
    <label class="muted"><input type="checkbox" id="f-follow" checked> 自动滚动</label>
    <label class="muted"><input type="checkbox" id="f-pause"> 暂停刷新</label>
    <button id="b-refresh">立即刷新</button>
    <span class="muted" id="p-url"></span>
  </div>
</header>
<div id="list"><div class="muted" style="padding:8px">正在加载…</div></div>
<footer class="muted" id="foot"></footer>
<script>
(function () {
  var state = { limit: 200, timer: null, nodes: {} };
  function esc(v) { return v === null || v === undefined ? "" : String(v); }
  function rowKey(ev) { return [ev.source, ev.pid, ev.seq].join("|"); }
  function statusOf(ev) { return ev.display_status || ev.status || "info"; }
  function badgeFor(status) {
    var map = { ok: "成功", error: "错误", running: "进行中", rolled_back: "已回滚",
                completed: "已完成", info: "信息" };
    return map[status] || status;
  }
  function buildBadge() {
    var span = document.createElement("span");
    span.className = "pill";
    return span;
  }
  function buildDetail(ev) {
    var det = document.createElement("div");
    det.className = "det";
    var err = document.createElement("div");
    err.className = "err";
    det.appendChild(err);
    var steps = document.createElement("ul");
    steps.className = "steps";
    det.appendChild(steps);
    var pre = document.createElement("pre");
    det.appendChild(pre);
    return { root: det, err: err, steps: steps, pre: pre };
  }
  function buildEvent(ev) {
    var div = document.createElement("div");
    div.className = "ev";
    var head = document.createElement("div");
    head.className = "head";
    head.title = "点击展开/折叠详细参数";
    var ts = document.createElement("span"); ts.className = "ts";
    var src = document.createElement("span"); src.className = "src";
    var name = document.createElement("span"); name.className = "evname";
    var tool = document.createElement("span"); tool.className = "tool";
    var dur = document.createElement("span"); dur.className = "dur";
    var badge = buildBadge();
    var rid = document.createElement("span"); rid.className = "muted";
    head.appendChild(ts); head.appendChild(src); head.appendChild(name); head.appendChild(tool);
    head.appendChild(dur); head.appendChild(badge); head.appendChild(rid);
    div.appendChild(head);
    var detail = buildDetail(ev);
    div.appendChild(detail.root);
    head.addEventListener("click", function () { div.classList.toggle("open"); });
    return { el: div, parts: { ts: ts, src: src, name: name, tool: tool, dur: dur,
                              badge: badge, rid: rid, detail: detail }, signature: null };
  }
  function updateEvent(entry, ev) {
    // Only touch the DOM when something actually changed: that keeps the expanded
    // state and the scroll position stable across the 1.5 s refreshes.
    var signature = JSON.stringify([statusOf(ev), ev.event, ev.tool || ev.command || "",
                                    ev.duration_ms, ev.ts, ev.operation_status,
                                    ev.message || ev.error || "", ev.steps || null]);
    if (entry.signature === signature) { return; }
    entry.signature = signature;
    var status = statusOf(ev);
    var parts = entry.parts;
    parts.ts.textContent = esc(ev.ts);
    parts.src.textContent = esc(ev.source);
    parts.name.textContent = esc(ev.event);
    parts.tool.textContent = esc(ev.tool || ev.command || "");
    parts.dur.textContent = (ev.duration_ms !== undefined && ev.duration_ms !== null)
      ? (ev.duration_ms + " ms") : "";
    parts.badge.className = "pill " + status;
    parts.badge.textContent = badgeFor(status);
    parts.rid.textContent = ev.request_id ? ("req " + String(ev.request_id).slice(0, 8)) : "";
    entry.el.className = "ev " + status + (entry.el.classList.contains("open") ? " open" : "");
    var detail = parts.detail;
    var message = ev.message || ev.error || "";
    detail.err.textContent = message ? (esc(ev.code ? (ev.code + ": ") : "") + esc(message)) : "";
    detail.err.style.display = message ? "block" : "none";
    var steps = ev.steps || (ev.result && ev.result.completed) || null;
    detail.steps.textContent = "";
    if (steps && steps.length) {
      detail.steps.style.display = "block";
      steps.slice(0, 100).forEach(function (s) {
        var li = document.createElement("li");
        var label = (s.index !== undefined ? ("#" + s.index + " ") : "") + (s.op || s.event || s.name || "step");
        li.textContent = label + " " + (s.ok === false ? "失败 " + (s.code || "") + " " + (s.error || "") : "完成");
        if (s.ok === false) { li.className = "err"; }
        detail.steps.appendChild(li);
      });
    } else {
      detail.steps.style.display = "none";
    }
    detail.pre.textContent = JSON.stringify(ev, null, 2);
  }
  function renderRows(events) {
    var list = document.getElementById("list");
    var seen = {};
    var empty = document.getElementById("empty-note");
    if (!events.length) {
      if (!empty) {
        empty = document.createElement("div");
        empty.id = "empty-note";
        empty.className = "muted";
        empty.style.padding = "8px";
        empty.textContent = "暂无匹配事件。";
        list.appendChild(empty);
      }
      return;
    }
    if (empty) { empty.remove(); }
    events.forEach(function (ev) {
      var key = rowKey(ev);
      seen[key] = 1;
      var entry = state.nodes[key];
      if (!entry) {
        entry = buildEvent(ev);
        state.nodes[key] = entry;
      }
      updateEvent(entry, ev);
      list.appendChild(entry.el);   // re-append keeps order, expansion and identity
    });
    Object.keys(state.nodes).forEach(function (key) {
      if (!seen[key]) {
        state.nodes[key].el.remove();
        delete state.nodes[key];
      }
    });
  }
  function refresh() {
    if (document.getElementById("f-pause").checked) { return; }
    var params = new URLSearchParams();
    params.set("limit", state.limit);
    var st = document.getElementById("f-status").value;
    var so = document.getElementById("f-source").value;
    var q = document.getElementById("f-query").value.trim();
    if (st) { params.set("status", st); }
    if (so) { params.set("source", so); }
    if (q) { params.set("q", q); }
    fetch("/api/events?" + params.toString(), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var counts = data.counts || {};
        document.getElementById("p-ok").textContent = "成功 " + (counts.ok || 0) + " 项";
        document.getElementById("p-error").textContent = "错误 " + (counts.error || 0) + " 项";
        document.getElementById("p-running").textContent = "进行中 " + (counts.running || 0) + " 项";
        document.getElementById("p-rolled").textContent = "已回滚 " + (counts.rolled_back || 0) + " 项";
        document.getElementById("p-meta").textContent =
          "操作合计 " + (counts.operations || 0) + "（读入 " + (data.window || 0) + " 条事件，返回 " +
          data.count + " / 匹配 " + data.matched + "）· 信息 info " + (counts.info || 0) +
          " · 已结束会话 " + (counts.ended_sessions || 0) + " · 日志文件 " + data.files +
          " · 服务器时间 " + data.ts;
        document.getElementById("p-url").textContent = data.viewer_url || "";
        document.getElementById("foot").textContent =
          "目录：" + data.root + " · 只读本地看板（仅回环监听，无外部依赖）· 图片仅显示大小与哈希，不显示 base64" +
          " · " + (counts.note || "");
        var follow = document.getElementById("f-follow").checked;
        renderRows(data.events);
        // Follow checked -> stick to the newest row. Follow unchecked -> leave the
        // scroll position (and every expanded row) exactly where the user left it.
        if (follow) { window.scrollTo(0, document.body.scrollHeight); }
      })
      .catch(function (err) {
        document.getElementById("p-meta").textContent = "刷新失败：" + err;
      });
  }
  ["f-status", "f-source"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", refresh);
  });
  document.getElementById("f-query").addEventListener("input", function () {
    clearTimeout(state.debounce); state.debounce = setTimeout(refresh, 250);
  });
  document.getElementById("b-refresh").addEventListener("click", refresh);
  state.timer = setInterval(refresh, 1500);
  refresh();
})();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "BlenderMCPLogViewer/" + str(VIEWER_VERSION)
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------- #
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        """Keep the console clean; the viewer must never spam stdout of a host."""

    def _loopback(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        try:
            import ipaddress

            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    # -- routing ----------------------------------------------------------- #
    def do_GET(self):  # noqa: N802 - stdlib API
        self._route()

    def do_HEAD(self):  # noqa: N802 - stdlib API
        self._route()

    def do_POST(self):  # noqa: N802 - stdlib API
        self._send(405, b'{"ok":false,"error":"read-only viewer: only GET is allowed"}', "application/json")

    def _route(self) -> None:
        if not self._loopback():
            self._send(403, b'{"ok":false,"error":"loopback only"}', "application/json")
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        viewer = self.server.viewer  # type: ignore[attr-defined]
        if path in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json(viewer.health())
        elif path == "/api/events":
            self._json(viewer.events_payload(query))
        elif path == "/api/stream":
            self._stream(viewer)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"ok": False, "error": "unknown endpoint", "endpoints": [
                "/", "/api/health", "/api/events", "/api/stream"]}, 404)

    def _stream(self, viewer) -> None:
        """Bounded SSE stream; the client reconnects, so nothing leaks."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        deadline = time.time() + SSE_MAX_SECONDS
        try:
            while time.time() < deadline:
                payload = viewer.events_payload({"limit": ["100"]})
                chunk = "data: %s\n\n" % json.dumps(payload, ensure_ascii=False, default=str)
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                time.sleep(SSE_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class LogViewer:
    """A read-only dashboard bound to one audit directory."""

    def __init__(
        self,
        directory: Path | str | None = None,
        host: str = DEFAULT_HOST,
        port: int = 0,
        owner: str = "mcp-server",
        publish: bool = True,
    ):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("the log viewer may only bind a loopback address")
        self.directory = Path(directory) if directory else audit.audit_dir()
        self.host = DEFAULT_HOST
        self.requested_port = int(port or 0)
        self.owner = owner
        self.publish = bool(publish)
        self.started_at = time.time()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.sanitizer = audit.get_audit().sanitizer

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> "LogViewer":
        if self._server is not None:
            return self
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            server = _Server((self.host, self.requested_port), _Handler)
        except OSError as exc:
            raise OSError(
                "cannot bind the log dashboard on %s:%s (%s); pass a free port or 0 for "
                "an OS-assigned one" % (self.host, self.requested_port or "auto", exc)
            ) from exc
        server.viewer = self  # type: ignore[attr-defined]
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(target=server.serve_forever, name="blender-mcp-log-viewer", daemon=True)
        self._thread.start()
        if self.publish:
            self.write_metadata()
        return self

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)

    @property
    def running(self) -> bool:
        return self._server is not None

    def stop(self) -> bool:
        server = self._server
        self._server = None
        if server is None:
            return False
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            pass
        self.remove_metadata()
        return True

    def __enter__(self) -> "LogViewer":
        return self.start()

    def __exit__(self, *exc_info) -> bool:
        self.stop()
        return False

    # -- metadata (race-safe discovery for other processes) ---------------- #
    @property
    def metadata_path(self) -> Path:
        return self.directory / ("viewer-%d.json" % os.getpid())

    def write_metadata(self) -> None:
        payload = {
            "service": SERVICE,
            "version": VIEWER_VERSION,
            "pid": os.getpid(),
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "root": str(self.directory),
            "owner": self.owner,
            "started_at": audit.now_iso(),
            "started_epoch": self.started_at,
        }
        target = self.metadata_path
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(target))
        except OSError:
            pass

    def remove_metadata(self) -> None:
        try:
            self.metadata_path.unlink()
        except OSError:
            pass

    # -- payloads ---------------------------------------------------------- #
    def health(self) -> dict:
        return {
            "ok": True,
            "service": SERVICE,
            "version": VIEWER_VERSION,
            "pid": os.getpid(),
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "root": str(self.directory),
            "owner": self.owner,
            "started_at": audit.now_iso(),
            "uptime_s": round(time.time() - self.started_at, 3),
            "read_only": True,
            "endpoints": ["/", "/api/health", "/api/events", "/api/stream"],
        }

    def events_payload(self, query: dict | None = None) -> dict:
        query = query or {}

        def first(name: str, default=None):
            values = query.get(name) or []
            return values[0] if values else default

        limit = first("limit", DEFAULT_LIMIT)
        try:
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        status = (first("status") or "").strip() or None
        source = (first("source") or "").strip() or None
        text = (first("q") or "").strip() or None
        # Read a wider window than we display: pairing a start with its end (and
        # spotting an ended session) needs the surrounding events, and "pending"
        # must never be guessed from a slice that only contains the starts.
        window = max(limit, OPERATION_WINDOW)
        raw = audit.tail_events(limit=window, source=source, query=text, directory=self.directory)
        annotated, summary = audit.annotate_operations(raw)
        if status:
            annotated = [entry for entry in annotated if entry.get("display_status") == status]
        events = annotated[-limit:]
        # Second pass: belt and braces against anything a foreign writer may have added.
        for entry in events:
            for key, value in list(entry.items()):
                entry[key] = self.sanitizer.value(value, key=key)
        info = audit.describe_directory(self.directory)
        return {
            "ok": True,
            "ts": audit.now_iso(),
            "viewer_url": self.url,
            "root": str(self.directory),
            "count": len(events),
            "matched": len(annotated),
            "limit": limit,
            "window": window,
            "files": info["files"],
            "counts": summary,
            "limits": info["limits"],
            "events": events,
        }


# --------------------------------------------------------------------------- #
# discovery / convenience helpers
# --------------------------------------------------------------------------- #
def metadata_files(directory: Path | str | None = None) -> list[Path]:
    root = Path(directory) if directory else audit.audit_dir()
    try:
        files = [p for p in root.glob("viewer-*.json") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def probe_viewer(url: str, timeout: float = 1.0) -> dict | None:
    """Return the ``/api/health`` payload of a live viewer, else ``None``."""
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a stale metadata file is expected
        return None
    if isinstance(payload, dict) and payload.get("service") == SERVICE:
        return payload
    return None


def find_live_viewer(directory: Path | str | None = None) -> dict | None:
    """Newest live viewer for this directory, from the per-process metadata files."""
    for path in metadata_files(directory):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = payload.get("url")
        if not url:
            continue
        health = probe_viewer(str(url))
        if health:
            return {**payload, **health, "metadata": str(path), "owned_by_this_process": health.get("pid") == os.getpid()}
    return None


def start_viewer(
    directory: Path | str | None = None,
    port: int = 0,
    reuse_live: bool = True,
    owner: str = "mcp-server",
) -> dict:
    """Start (or reuse) a dashboard and return a JSON-friendly description."""
    if reuse_live:
        live = find_live_viewer(directory)
        if live:
            return {"started": False, "reused": True, **live}
    viewer = LogViewer(directory=directory, port=port, owner=owner).start()
    _register_owned(viewer)
    payload = viewer.health()
    payload.update({"started": True, "reused": False, "owned_by_this_process": True})
    return payload


#: Viewers started *by this process*; only these may ever be stopped by it.
_OWNED: list[LogViewer] = []
_OWNED_LOCK = threading.Lock()


def _register_owned(viewer: LogViewer) -> None:
    with _OWNED_LOCK:
        _OWNED.append(viewer)


def owned_viewers() -> list[LogViewer]:
    with _OWNED_LOCK:
        return list(_OWNED)


def stop_owned_viewers() -> int:
    """Stop every dashboard this process started. Never touches a foreign one."""
    with _OWNED_LOCK:
        viewers = list(_OWNED)
        _OWNED.clear()
    stopped = 0
    for viewer in viewers:
        if viewer.stop():
            stopped += 1
    return stopped


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# standalone entry point (hidden service started by launch_log_viewer.ps1)
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Blender MCP audit log dashboard (read-only, loopback)")
    parser.add_argument("--root", default=None, help="audit log directory (default <MCP>/.runtime/logs)")
    parser.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument("--seconds", type=float, default=0.0, help="stop after N seconds (0 = until killed)")
    parser.add_argument("--quiet", action="store_true", help="only print the URL line")
    args = parser.parse_args(argv)

    viewer = LogViewer(directory=args.root, port=args.port, owner="standalone").start()
    print("LOG_VIEWER_URL %s" % viewer.url, flush=True)
    if args.open:
        try:
            webbrowser.open(viewer.url)
        except Exception:  # noqa: BLE001 - a headless host simply has no browser
            pass
    if not args.quiet:
        print("[log-viewer] root: %s" % viewer.directory, file=sys.stderr)
        print("[log-viewer] read-only dashboard on %s (Ctrl-C to stop)" % viewer.url, file=sys.stderr)
    try:
        if args.seconds and args.seconds > 0:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        viewer.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual/PS launch path
    raise SystemExit(main())
