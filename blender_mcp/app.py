"""Application entry point for the Blender MCP stdio server."""

from __future__ import annotations

import logging
import os
import sys

from . import __version__, config
from .bridge import BridgeClient
from .tools import build_server

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
    bridge = BridgeClient()
    mcp = build_server(bridge, name="blender")
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:  # pragma: no cover - interactive Ctrl-C
        LOG.info("interrupted; closing bridge connection")
        bridge.close()
        return 130
    finally:
        bridge.close()
    return 0
