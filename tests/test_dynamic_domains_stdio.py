"""Production stdio: directory views never filter list/call or require refresh."""
from __future__ import annotations
import asyncio
import functools
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from stdio_pipe_transport import mcp_stdio_session
from blender_mcp import tool_profiles as tp

RUNTIME = ROOT / ".runtime/domain_browsing/protocol"

def env(profile=None, identity="client"):
    result = dict(os.environ, BLENDER_MCP_PORT="1", BLENDER_MCP_TIMEOUT="0.2",
                  BLENDER_MCP_TOKEN_FILE=str(RUNTIME / "fixture_token"),
                  BLENDER_MCP_AUDIT_DIR=str(RUNTIME / identity))
    for key in (tp.PROFILE_ENV_VAR, tp.LOCAL_DOMAINS_ENV_VAR, tp.VIEW_DOMAINS_ENV_VAR):
        result.pop(key, None)
    if profile is not None:
        result[tp.PROFILE_ENV_VAR] = profile
    return result

def payload(result):
    return result.structuredContent or json.loads(next(c.text for c in result.content if hasattr(c,"text")))

def text_of(result):
    return " ".join(getattr(c,"text","") for c in result.content)

def with_server(method):
    @functools.wraps(method)
    async def bounded(self):
        async with asyncio.timeout(90):
            async with self.context as (self.session, self.process):
                await method(self)
    return bounded

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.notifications = []
        self.initialized = []
        async def observe(message):
            if getattr(getattr(message,"root",message),"method",None) == "notifications/tools/list_changed":
                self.notifications.append(message)
        self.context = mcp_stdio_session(sys.executable, [str(ROOT / "server.py")], env=env(), cwd=ROOT,
            message_handler=observe, initialization_callback=self.initialized.append,
            stderr_path=RUNTIME / "server.stderr.log")

    async def listing(self):
        return {t.name:t.model_dump(mode="json",by_alias=True,exclude_none=True)
                for t in (await self.session.list_tools()).tools}

    @with_server
    async def test_all_tools_permanent_and_views_need_no_notification(self):
        self.assertFalse(self.initialized[0].capabilities.tools.listChanged)
        initial = await self.listing()
        self.assertEqual(set(initial), tp.KNOWN_TOOL_NAMES)
        pid = self.process.pid
        for mode, domains in (("replace",["properties"]),("add",["geometry_nodes"]),
                              ("remove",["properties"]),("replace",[])):
            result = await self.session.call_tool("blender_set_tool_domains", {"mode":mode,"domains":domains})
            self.assertFalse(result.isError, text_of(result))
            self.assertEqual(payload(result)["available_tool_count"], 93)
            self.assertFalse(payload(result)["registration_or_call_permissions_changed"])
            self.assertEqual(await self.listing(), initial)
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.process.pid, pid)
        report = {"sdk":"mcp==1.30.0","production_server":True,"protocol_tool_count":93,
                  "all_tools_permanent_across_views":True,"list_changed_capability":False,
                  "list_notifications_received":0,"same_stdio_pid":pid,
                  "no_client_refresh_needed_for_directory_browsing":True,"ok":True}
        (RUNTIME / "report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")

    @with_server
    async def test_interface_queries_exact_schemas_without_selecting(self):
        all_tools = await self.listing()
        original90 = json.loads((ROOT / "tests/fixtures/legacy90_tools.json").read_text(encoding="utf-8"))
        for name, spec in original90.items():
            self.assertEqual(all_tools[name], spec)
        initial = payload(await self.session.call_tool("blender_tool_domain_state",{}))
        for domain in tp.DOMAINS:
            reply = await self.session.call_tool("blender_tool_domains", {"domains":[domain]})
            self.assertFalse(reply.isError, text_of(reply))
            directory = payload(reply)
            self.assertEqual({t["name"] for t in directory["tools"]},set(tp.DOMAIN_TOOLS[domain]))
            for spec in directory["tools"]:
                self.assertEqual(spec, all_tools[spec["name"]])
            self.assertEqual((payload(await self.session.call_tool("blender_tool_domain_state",{})))["revision"], initial["revision"])
        self.assertEqual(await self.listing(), all_tools)

    @with_server
    async def test_other_domain_direct_calls_reach_business_checks(self):
        await self.session.call_tool("blender_set_tool_domains", {"mode":"replace","domains":["outliner"]})
        # Full argument validation/routing, not a domain-denied error.
        for tool, args in (("blender_list_node_types",{}),
                           ("blender_properties_list_parameters",{"object":"Fixture"})):
            result = await self.session.call_tool(tool,args)
            self.assertTrue(result.isError)  # intentionally offline bridge
            self.assertIn("bridge_unavailable", text_of(result))
            self.assertNotIn("tool_domain_disabled", text_of(result))
        opened = await self.session.call_tool("blender_open_blend",
            {"relative_path":"never.blend","confirm_replace_current":False})
        saved = await self.session.call_tool("blender_save_blend",{"relative_path":"../escape.blend"})
        launch = await self.session.call_tool("blender_launch_blender",{"wait_seconds":0})
        for result in (opened,saved,launch):
            self.assertTrue(result.isError)
            self.assertNotIn("tool_domain_disabled",text_of(result))
        self.assertEqual(set(await self.listing()), tp.KNOWN_TOOL_NAMES)

    @with_server
    async def test_bad_selection_does_not_change_view_or_availability(self):
        await self.session.call_tool("blender_set_tool_domains", {"mode":"replace","domains":["properties"]})
        before = payload(await self.session.call_tool("blender_tool_domain_state",{}))
        for domains, code in ((["bad"],"unknown_domain"),(["properties","properties"],"duplicate_domain"),
                              (["properties"]*12,"invalid_domain_selection")):
            result = await self.session.call_tool("blender_set_tool_domains",{"mode":"replace","domains":domains})
            self.assertTrue(result.isError)
            self.assertEqual(result.structuredContent["code"],code)
            self.assertEqual(payload(await self.session.call_tool("blender_tool_domain_state",{})),before)
        self.assertEqual(set(await self.listing()), tp.KNOWN_TOOL_NAMES)

    @with_server
    async def test_two_clients_independent_directory_views_all_tools_callable(self):
        async with mcp_stdio_session(sys.executable,[str(ROOT/"server.py")],env=env(identity="other"),cwd=ROOT) as (other,process):
            self.assertNotEqual(process.pid,self.process.pid)
            replies = await asyncio.gather(
                self.session.call_tool("blender_set_tool_domains",{"mode":"replace","domains":["properties"]}),
                other.call_tool("blender_set_tool_domains",{"mode":"replace","domains":["outliner"]}))
            self.assertTrue(all(not r.isError for r in replies))
            self.assertEqual(payload(await self.session.call_tool("blender_tool_domain_state",{}))["selected_domains"],["properties"])
            self.assertEqual(payload(await other.call_tool("blender_tool_domain_state",{}))["selected_domains"],["outliner"])
            self.assertEqual(set(await self.listing()),tp.KNOWN_TOOL_NAMES)
            self.assertEqual({t.name for t in (await other.list_tools()).tools},tp.KNOWN_TOOL_NAMES)
            result = await other.call_tool("blender_properties_list_parameters",{"object":"Fixture"})
            self.assertIn("bridge_unavailable",text_of(result))
            self.assertNotIn("tool_domain_disabled",text_of(result))

class StartupProtocolTests(unittest.TestCase):
    def test_initial_views_never_filter_protocol_list(self):
        RUNTIME.mkdir(parents=True,exist_ok=True)
        async def verify():
            for domains in ([],["properties"],["geometry_nodes","properties","model_inspection"]):
                configured = env()
                configured[tp.VIEW_DOMAINS_ENV_VAR] = json.dumps(domains)
                async with mcp_stdio_session(sys.executable,[str(ROOT/"server.py")],env=configured,cwd=ROOT) as (session,_):
                    self.assertEqual({t.name for t in (await session.list_tools()).tools},tp.KNOWN_TOOL_NAMES)
                    state = payload(await session.call_tool("blender_tool_domain_state",{}))
                    self.assertEqual(set(state["selected_domains"]),set(domains))
                    self.assertEqual(state["available_tool_count"],93)
        asyncio.run(asyncio.wait_for(verify(),25))

    def test_invalid_configuration_rejected(self):
        for profile in (""," ","typo"):
            result = subprocess.run([sys.executable,str(ROOT/"server.py")],env=env(profile),cwd=ROOT,
                input="",text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,"")
            self.assertNotIn("Traceback",result.stderr)

if __name__ == "__main__":
    unittest.main()
