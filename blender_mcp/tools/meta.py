"""Meta tools: health / version / bridge status."""

from __future__ import annotations

import platform
import sys

from mcp.server.fastmcp import FastMCP

from .. import config
from ..bridge import BridgeClient, BridgeError
from ._common import call


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_health",
        description=(
            "Health check of the local Blender bridge and Blender itself: bridge status, protocol "
            "version, Blender version, Python version, PID, loaded .blend, scene name, object and node "
            "group counts, and the list of bridge commands. Call this first: it proves the MCP server "
            "can reach Blender and tells you the exact Blender version whose RNA you are about to use."
        ),
    )
    def blender_health() -> dict:
        payload = call(bridge, "health")
        payload["mcp_server"] = {
            "python": sys.executable,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "bridge_client_address": bridge.address,
            "calls_made": bridge.calls,
        }
        payload["environment"] = config.describe_environment()
        return payload

    @mcp.tool(
        name="blender_server_status",
        description=(
            "Counters of the Blender-side socket loop: running flag, address, connected clients, "
            "successful/failed/unauthorized request counts and the last error. Use it to diagnose "
            "'Blender is busy', auth problems or stalled clients."
        ),
    )
    def blender_server_status() -> dict:
        return call(bridge, "server_status")

    @mcp.tool(
        name="blender_list_commands",
        description=(
            "List every low-level bridge command the running Blender exposes, with a mutating flag. "
            "Useful to confirm the add-on version loaded in Blender matches this MCP server."
        ),
    )
    def blender_list_commands() -> dict:
        return call(bridge, "list_commands")

    @mcp.tool(
        name="blender_ping",
        description="Cheapest possible round trip to the Blender bridge (no scene access).",
    )
    def blender_ping() -> dict:
        return call(bridge, "ping", timeout=5.0)

    @mcp.tool(
        name="blender_bridge_probe",
        description=(
            "Probe the configured bridge address without raising: returns reachable=true/false plus the "
            "health payload or the error text. Use it before long workflows to decide whether a Blender "
            "has to be launched."
        ),
    )
    def blender_bridge_probe() -> dict:
        healthy, payload = bridge.is_healthy(timeout=3.0)
        result = {
            "reachable": healthy,
            "address": bridge.address,
            "token_present": config.token_path().exists(),
        }
        if healthy:
            result["health"] = payload
        else:
            result["hint"] = (
                "No healthy bridge answered. Launch Blender with blender_launch_blender, or run "
                "MCP/blender/bootstrap.py inside an open Blender."
            )
        return result

    _ = BridgeError  # imported for symmetry / documentation purposes
