"""Shared plumbing for the MCP tool modules."""

from __future__ import annotations

from mcp.server.fastmcp.exceptions import ToolError

from ..bridge import BlenderCommandError, BridgeClient, BridgeError

#: Instructions returned to Codex when a tool cannot even reach Blender.
START_HINT = (
    "Start Blender with the bundled bootstrap (MCP/launch_blender.ps1, or "
    "`blender.exe --python MCP/blender/bootstrap.py`), or call blender_launch_blender "
    "from this MCP server. Headless: add `-- --mcp-pump` and run with `--background`."
)


def call(bridge: BridgeClient, command: str, params: dict | None = None, timeout: float | None = None) -> dict:
    """Forward one command to Blender, converting failures into MCP tool errors.

    A failing tool must be an error (``isError``), never a success payload that
    happens to contain "error" - the caller (an LLM) must be able to rely on the
    error channel.
    """
    try:
        return bridge.call(command, params or {}, timeout=timeout)
    except BlenderCommandError as exc:
        raise ToolError(exc.as_tool_message()) from exc
    except BridgeError as exc:
        raise ToolError("%s [%s]. %s" % (exc.message, exc.code, START_HINT)) from exc


def require_bridge_health(bridge: BridgeClient, timeout: float = 3.0) -> dict:
    return call(bridge, "health", {}, timeout=timeout)


def error_payload(exc: Exception) -> str:
    if isinstance(exc, ToolError):
        return str(exc)
    return "%s: %s" % (type(exc).__name__, exc)
