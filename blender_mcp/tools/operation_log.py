"""Audit-log tools: read the operation log and drive the live dashboard.

Two tools, both local and read-only with respect to Blender:

* ``blender_operation_log`` - the newest audited events (every MCP tool call,
  every bridge command, batch steps and rollbacks) with filters. It reads only
  the fixed ``<MCP>/.runtime/logs`` directory; the caller cannot name a path.
* ``blender_log_dashboard`` - start / inspect / stop the local live dashboard and
  return its URL. Starting it never opens a browser unless ``open_browser=true``
  is passed explicitly, so a plain stdio server session stays headless.

Neither tool can reach Blender's token: audit entries are sanitised when they are
written and sanitised again on the way out here.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from .. import audit, log_viewer
from ..bridge import BridgeClient


def _compact(events: list[dict], limit: int) -> list[dict]:
    """Trim each event to the fields a reader actually needs."""
    keep = ("ts", "source", "event", "status", "request_id", "tool", "command", "duration_ms",
            "ok", "code", "message", "steps", "transaction", "committed", "rolled_back", "args", "result")
    out = []
    for entry in events[-limit:]:
        out.append({key: entry[key] for key in keep if key in entry})
    return out


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    _ = bridge  # the audit log is local; Blender is not required to read it

    @mcp.tool(
        name="blender_operation_log",
        description=(
            "Read the persistent audit log of every MCP operation: each tool call with its sanitised "
            "parameters, outcome (ok / error / running / rolled_back), error code and message, duration in "
            "milliseconds, plus the Blender bridge command it produced and the per-step results of a batched "
            "graph edit (with the transaction verdict). Newest events come last. Filters: status, source "
            "('mcp' or 'bridge') and a free-text query. Secrets are removed before they are written, and "
            "image payloads are stored as a size + hash - never as base64. The directory is fixed at "
            "<MCP>/.runtime/logs and cannot be chosen by the caller."
        ),
    )
    def blender_operation_log(
        limit: int = 50,
        status: Literal["ok", "error", "running", "rolled_back", "info"] | None = None,
        source: Literal["mcp", "bridge"] | None = None,
        query: str | None = None,
        include_details: bool = False,
    ) -> dict:
        limit = max(1, min(int(limit or 50), 500))
        events = audit.tail_events(limit=limit, status=status, source=source, query=query)
        payload = {
            "directory": str(audit.audit_dir()),
            "status_filter": status,
            "source_filter": source,
            "query": query,
            "count": len(events),
            "counts": audit.status_counts(events),
            "limits": audit.describe_directory()["limits"],
            "events": events if include_details else _compact(events, limit),
            "note": (
                "this is the log of THIS MCP server and of the Blender bridge it talked to; the live, "
                "auto-refreshing view is returned by blender_log_dashboard"
            ),
        }
        return payload

    @mcp.tool(
        name="blender_log_dashboard",
        description=(
            "Start (hidden, in this process), inspect or stop the local live dashboard that shows every "
            "audited operation in real time - 成功 / 错误 / 进行中 / 已回滚 - with status filters, "
            "auto-scroll and expandable parameters. It is a read-only page served from Python's standard "
            "library on 127.0.0.1 only; there is no CDN dependency, no file access over HTTP and no token "
            "in the page. action='start' returns the URL to open; open_browser only opens it when explicitly "
            "set to true. action='status' reports a live viewer without starting one, action='stop' stops "
            "only a viewer this server process started."
        ),
    )
    def blender_log_dashboard(
        action: Literal["start", "status", "stop"] = "start",
        port: int = 0,
        open_browser: bool = False,
        reuse: bool = True,
    ) -> dict:
        if action == "status":
            live = log_viewer.find_live_viewer()
            if not live:
                return {
                    "running": False,
                    "directory": str(audit.audit_dir()),
                    "hint": "call blender_log_dashboard with action='start' to get a live URL",
                }
            return {"running": True, "url": live["url"], "pid": live.get("pid"),
                    "owned_by_this_process": live.get("owned_by_this_process"),
                    "directory": live.get("root"), "service": live.get("service"),
                    "uptime_s": live.get("uptime_s")}

        if action == "stop":
            stopped = log_viewer.stop_owned_viewers() > 0
            live = log_viewer.find_live_viewer()
            return {
                "stopped": stopped,
                "running": bool(live),
                "url": live["url"] if live else None,
                "note": (
                    "only a dashboard started by this MCP server process is stopped; a viewer started by "
                    "MCP/launch_log_viewer.ps1 or by another process is left running on purpose"
                ),
            }

        info = log_viewer.start_viewer(port=max(0, min(int(port or 0), 65535)), reuse_live=bool(reuse))
        url = info.get("url")
        if open_browser and url:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a headless host has no browser
                pass
        return {
            "running": True,
            "started": info.get("started"),
            "reused": info.get("reused"),
            "url": url,
            "pid": info.get("pid"),
            "host": info.get("host"),
            "port": info.get("port"),
            "directory": info.get("root"),
            "owner": info.get("owner"),
            "read_only": True,
            "browser_opened": bool(open_browser),
            "usage": {
                "open": "open the returned url in a browser to watch operations live",
                "standalone": "powershell -ExecutionPolicy Bypass -File MCP\\launch_log_viewer.ps1 -Open",
                "read": "the same data is available as JSON via GET <url>api/events?limit=200",
            },
            "hint": (
                "the dashboard lives in this MCP server process, so it stops when the server exits; "
                "launch_log_viewer.ps1 starts an independent hidden viewer instead"
            ),
        }
