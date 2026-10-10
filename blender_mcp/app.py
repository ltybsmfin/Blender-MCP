"""Application entry point for the Blender MCP stdio server."""

from __future__ import annotations

import logging
import os
import sys

from . import __version__, audit, config
from .bridge import BridgeClient
from .tools import build_server
from .tool_profiles import ToolProfileError

LOG = logging.getLogger("blender-mcp")


def configure_logging() -> None:
    """Log to stderr (never stdout) - stdout carries the MCP protocol only."""
    level = os.environ.get("BLENDER_MCP_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("[blender-mcp] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))


def main() -> int:
    configure_logging()
    config.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log = audit.get_audit()
    token = config.ensure_token()
    LOG.info(
        "starting blender-mcp %s | python=%s | bridge=%s | workspace=%s | token_file=%s (present=%s)",
        __version__,
        sys.executable,
        config.bridge_address(),
        config.WORKSPACE_ROOT,
        config.token_path(),
        bool(token),
    )
    log.record(
        "session_start",
        tool=None,
        version=__version__,
        python=sys.executable,
        bridge=config.bridge_address(),
        workspace=str(config.WORKSPACE_ROOT),
        audit=log.describe(),
    )
    bridge = BridgeClient()
    try:
        mcp = build_server(bridge, name="blender")
    except ToolProfileError as exc:
        LOG.error("tool_profile_configuration_error: %s", exc)
        log.record("session_stop", reason="tool_profile_configuration_error", error=str(exc))
        log.close()
        bridge.close()
        return 2
    LOG.info("tool profile=%s | tools=%d", mcp.tool_profile.profile_name, mcp.tool_profile.kept_count)
    log.record("tool_profile", **mcp.tool_profile.describe())
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
        LOG.info("interrupted; closing bridge connection")
        log.record("session_stop", reason="interrupted")
        bridge.close()
        return 130
    finally:
        log.record("session_stop", reason="eof")
        log.close()
        bridge.close()
    return 0
