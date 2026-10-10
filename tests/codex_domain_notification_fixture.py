"""Test-only production domain server with a fixed local notification trigger.

Never installed as a plugin. No tools are added, no Blender command is sent.
The trigger lets a real Codex client be tested without creating a chat or using
model turns. Public handlers/session notification API only.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from blender_mcp.tools import build_server

RUNTIME = ROOT / ".runtime/dynamic_domains/codex_notification"
RUNTIME.mkdir(parents=True, exist_ok=True)
server = build_server(name="blender_domain_probe", profile="global-only")
queries = []
workers = []

def record():
    (RUNTIME / "fixture_state.json").write_text(json.dumps({
        "pid": os.getpid(), "list_queries": queries,
        "domains": list(server.domain_controller.selection.domains),
        "visible_count": len(server.domain_controller.selection.visible),
        "revision": server.domain_controller.selection.revision,
        "notification_sent": server.domain_controller.selection.notification_sent,
    }, indent=2), encoding="utf-8")

async def change_once(session):
    for _ in range(600):  # bounded one-minute fixture, never an endless worker
        if (RUNTIME / "trigger").exists():
            await server.domain_controller.set_domains("replace", ["properties"], session.send_tool_list_changed)
            record()
            return
        await asyncio.sleep(.1)

@server.protocol.list_tools()
async def list_tools():
    queries.append({"domains": list(server.domain_controller.selection.domains),
                    "revision": server.domain_controller.selection.revision})
    if len(queries) == 1:
        workers.append(asyncio.create_task(change_once(server.protocol.request_context.session)))
    record()
    return await server.list_tools()

if __name__ == "__main__":
    server.run()
