"""Bounded, non-mutating bridge diagnosis and duplicate-launch guards."""

from __future__ import annotations

import json
import os
import socket
import subprocess

from mcp.server.fastmcp.exceptions import ToolError

from .. import config
from ..bridge import BridgeError


def probe_bridge(bridge, timeout: float = 3.0) -> dict:
    """Keep the failure category instead of treating every error as 'not started'."""
    result = {"reachable": False, "address": bridge.address}
    try:
        health = bridge.health(timeout=timeout)
    except BridgeError as exc:
        code = getattr(exc, "remote_code", exc.code)
        stage = {
            "bridge_unavailable": "connection",
            "bridge_timeout": "response_timeout",
            "bridge_protocol_error": "protocol",
            "unauthorized": "authentication",
            "token_unavailable": "token_file",
        }.get(code, "bridge_command")
        result.update(stage=stage, error_code=code, error=exc.as_tool_message())
        # Do not print credentials or automatically rewrite the token/configuration.
        result["hint"] = {
            "authentication": "Check that both processes use the same token FILE; do not print or replace it.",
            "token_file": "Check token file presence and permissions; do not print its contents.",
            "response_timeout": "The bridge may be busy. Wait and retry a read-only health check; do not launch another Blender.",
            "protocol": "Check the bootstrap/add-on version and listener on this address; do not launch another Blender.",
        }.get(stage, "Check existing Blender processes and this port before launching. An open Blender needs its bridge bootstrap.")
        return result
    if not isinstance(health, dict) or health.get("bridge") != "blender-mcp" or health.get("protocol") != 1:
        result.update(stage="protocol", error_code="bridge_protocol_error",
                      error="Health response is not from a compatible Blender MCP bridge.",
                      hint="Inspect the listener/add-on version; do not launch a second process on this port.")
        return result
    result.update(reachable=True, stage="ready", health=health)
    return result


def blender_processes() -> list[dict]:
    """Inventory PIDs only (no command lines or environment secrets). Fail closed."""
    try:
        if os.name == "nt":
            command = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                "$items = @(Get-Process -Name blender -ErrorAction SilentlyContinue | "
                "Select-Object @{Name='pid';Expression={$_.Id}}); "
                "ConvertTo-Json -InputObject $items -Compress"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=True,
            )
            values = json.loads(completed.stdout.strip() or "[]")
            if not isinstance(values, list):
                raise ValueError("process inventory is not a list")
            return [{"pid": int(row["pid"])} for row in values]
        completed = subprocess.run(
            ["ps", "-eo", "pid=,comm="], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=5, text=True, check=True,
        )
        rows = []
        for line in completed.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and os.path.basename(fields[1]).lower() in ("blender", "blender.exe"):
                rows.append({"pid": int(fields[0])})
        return rows
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("launch_state_unknown: could not safely check existing Blender processes (%s); nothing launched" % type(exc).__name__) from exc


def assert_launch_idle(bridge, diagnosis: dict) -> None:
    """A failed health check is insufficient permission to start a second Blender."""
    if diagnosis.get("reachable"):
        raise ToolError("bridge_already_running: reuse_existing=false cannot start a duplicate on %s" % bridge.address)
    if diagnosis.get("stage") != "connection":
        raise ToolError("launch_blocked: %s at %s; resolve the existing bridge error first (nothing launched)" % (diagnosis.get("error_code"), bridge.address))
    try:
        # Windows loopback refusal can take about two seconds. Reuse the client's
        # bounded connect budget; a 0.5s check misclassified an idle port as busy.
        with socket.create_connection((bridge.host, bridge.port), timeout=config.CONNECT_TIMEOUT):
            raise ToolError("port_in_use: a listener exists on %s but did not pass health; nothing launched" % bridge.address)
    except ConnectionRefusedError:
        pass
    except ToolError:
        raise
    except OSError as exc:
        # A timeout/unknown transport error does not prove the port is unoccupied.
        raise ToolError("launch_state_unknown: cannot establish that %s is idle (%s); nothing launched" % (bridge.address, type(exc).__name__)) from exc
    processes = blender_processes()
    if processes:
        raise ToolError(
            "blender_without_bridge: existing Blender PID(s) %s but no healthy bridge on %s; "
            "run MCP/blender/bootstrap.py in that Blender or start its installed bridge panel. "
            "No process was launched, closed, or modified." % (", ".join(str(row["pid"]) for row in processes), bridge.address)
        )
