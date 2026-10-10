"""Windows helper entry point; use the named MCP launcher, never a second bridge client."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp.server.fastmcp.exceptions import ToolError

from . import audit, config
from .bridge import BridgeClient
from .tools import build_server


async def run(options) -> int:
    # This standalone helper owns the launch. A temporary SDK stdio client's
    # Windows Job Object kills descendant Blender processes on client shutdown.
    # Dispatch the SAME named/typed/audited FastMCP tool locally instead; do not
    # create that disposable parent server/job or change SDK cleanup behavior.
    os.environ.update(BLENDER_MCP_HOST=options.host, BLENDER_MCP_PORT=str(options.port))
    if options.isolate_config:
        isolated = config.RUNTIME_DIR / "blender_config"
        isolated.mkdir(parents=True, exist_ok=True)
        os.environ["BLENDER_USER_CONFIG"] = str(isolated)
        # Official Blender 5.2 --help exposes BLENDER_USER_EXTENSIONS. Keep
        # optional extension cache within Mod for the project-owned profile.
        extension_cache = config.WORKSPACE_ROOT / "Mod" / "blender_mcp_node_notes" / ".runtime" / "blender_extensions"
        extension_cache.mkdir(parents=True, exist_ok=True)
        os.environ["BLENDER_USER_EXTENSIONS"] = str(extension_cache)
    extra_args = ["--mcp-pump-seconds", str(options.pump_seconds)] if options.pump_seconds else []
    bridge = BridgeClient()
    mcp = build_server(bridge)
    log = audit.get_audit()
    log.record("session_start", source_kind="launch_helper")
    try:
        async with asyncio.timeout(options.wait_seconds + 25):
            result = await mcp.call_tool("blender_launch_blender", {
                "background": options.background, "factory_startup": options.factory_startup,
                "wait_seconds": options.wait_seconds, "reuse_existing": True,
                "blender_path": options.blender_path, "extra_args": extra_args,
            })
            data = audit.result_mapping(result)
            if not data:
                raise ToolError("launch_result_invalid: named launcher returned no metadata")
            health = data.get("health") or {}
            print(json.dumps({"launched": data.get("launched"), "reused": data.get("reused"),
                              "pid": data.get("pid"), "address": health.get("server", {}).get("address") or data.get("address"),
                              "blender_version": health.get("blender_version") or data.get("blender_version"),
                              "log_file": data.get("log_file")}, ensure_ascii=False))
            return 0
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        bridge.close()
        log.record("session_stop", reason="launch_helper_complete")
        log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--factory-startup", action="store_true")
    parser.add_argument("--isolate-config", action="store_true")
    parser.add_argument("--blender-path")
    parser.add_argument("--host", default=config.bridge_host())
    parser.add_argument("--port", type=int, default=config.bridge_port())
    parser.add_argument("--pump-seconds", type=float, default=0)
    parser.add_argument("--wait-seconds", type=float, default=30)
    options = parser.parse_args()
    if not 1 <= options.wait_seconds <= 60:
        parser.error("wait-seconds must be between 1 and 60")
    try:
        return asyncio.run(run(options))
    except (TimeoutError, OSError) as exc:
        print("Launcher failed (%s). Retry health and inspect existing processes before launching again." % type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
