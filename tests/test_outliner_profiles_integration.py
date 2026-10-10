"""Actual server.py stdio profiles; no Blender connection or scene mutation."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from stdio_pipe_transport import mcp_stdio_session
from blender_mcp.tool_profiles import KNOWN_TOOL_NAMES, resolve_profile

RUNTIME = ROOT / ".runtime" / "outliner_profiles_integration"


def environment(profile):
    return dict(os.environ, BLENDER_MCP_PROFILE=profile, BLENDER_MCP_PORT="1",
                BLENDER_MCP_TOKEN_FILE=str(RUNTIME / "fixture_token"),
                BLENDER_MCP_AUDIT_DIR=str(RUNTIME / "audit"))


async def verify():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    result = {"kind": "real_server_stdio_no_blender", "profiles": {}}
    baseline = json.loads((ROOT / ".runtime" / "outliner_schema_baseline.json").read_text(encoding="utf-8"))
    schemas, pids = None, set()
    os.environ.pop("BLENDER_MCP_LOCAL_DOMAINS", None)
    os.environ.pop("BLENDER_MCP_VIEW_DOMAINS", None)
    for profile in ("all", "core", "outliner", "geometry_nodes", "algorithm_reproduction"):
        async with mcp_stdio_session(sys.executable, [str(ROOT / "server.py")],
                                    env=environment(profile), cwd=ROOT,
                                    stderr_path=RUNTIME / (profile + ".stderr.log")) as (session, process):
            listed = await asyncio.wait_for(session.list_tools(), 15)
            tools = {tool.name: tool for tool in listed.tools}
            expected = KNOWN_TOOL_NAMES if profile == "all" else resolve_profile(profile).tool_names
            assert set(tools) == set(expected), (profile, sorted(set(tools) ^ set(expected)))
            if process:
                assert process.pid not in pids
                pids.add(process.pid)
            shape = {name: {"inputSchema": tool.inputSchema, "outputSchema": tool.outputSchema}
                     for name, tool in tools.items()}
            if profile == "all":
                schemas = shape
                assert len(baseline) == 61
                assert all(shape[name] == value for name, value in baseline.items())
            else:
                assert all(value == schemas[name] for name, value in shape.items())
            view_reply = await session.call_tool("blender_tool_domain_state", {})
            view = view_reply.structuredContent or json.loads(next(c.text for c in view_reply.content if hasattr(c, "text")))
            assert view["all_tools_active"] is True
            assert view["available_tool_count"] == 93
            assert set(view["view_tool_names"]) == resolve_profile(profile).view_tool_names
            assert set(view["selected_domains"]) == set(resolve_profile(profile).domains)
            assert not (await session.call_tool("blender_environment", {})).isError
            result["profiles"][profile] = {"count": len(tools), "names": sorted(tools),
                                           "directory_view_count": view["view_tool_count"], "all_tools_active": True,
                                           "pid": process.pid if process else None}
    invalid = subprocess.run([sys.executable, str(ROOT / "server.py")], cwd=ROOT,
                             env=environment("__invalid_profile__"), input="", capture_output=True,
                             text=True, timeout=15)
    assert invalid.returncode == 2 and "tool_profile_configuration_error" in invalid.stderr
    assert "Traceback" not in invalid.stderr and not invalid.stdout.strip()
    result.update(ok=True, original_schemas_preserved=len(baseline), distinct_server_processes=len(pids),
                  invalid_profile_exit_code=invalid.returncode)
    (RUNTIME / "report.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "counts": {k: v["count"] for k, v in result["profiles"].items()},
                      "original_schemas_preserved": len(baseline)}, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(verify())
