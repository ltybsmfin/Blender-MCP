"""Directory views are discovery data, never business-tool permissions."""
from __future__ import annotations
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from blender_mcp import tool_profiles as tp
from blender_mcp.tools import build_server


class RecordingBridge:
    address = "127.0.0.1:1"
    def __init__(self):
        self.calls = []
    def call(self, command, params=None, **kwargs):
        self.calls.append((command, params))
        return {"audit_protocol": 1, "fixture": command}


class MappingTests(unittest.TestCase):
    def test_complete_exclusive_mapping(self):
        names = [n for group in tp.DOMAIN_TOOLS.values() for n in group]
        self.assertEqual(len(names), 93)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(tp.validate_registered(names), tp.KNOWN_TOOL_NAMES)
        self.assertTrue(tp.CONTROL_TOOL_NAMES <= tp.GLOBAL_TOOL_NAMES)

    def test_profiles_never_restrict_tools(self):
        for profile in tp.PROFILE_NAMES:
            resolved = tp.resolve_profile(profile)
            self.assertEqual(resolved.tool_names, tp.KNOWN_TOOL_NAMES)
            self.assertEqual(resolved.tool_count, 93)
            self.assertFalse(resolved.restricted)
        self.assertEqual(tp.allowed_tool_names(), tp.KNOWN_TOOL_NAMES)
        self.assertEqual(tp.resolve_profile(environ={}).domains, ())

    def test_view_configuration_and_priority(self):
        configured = {tp.VIEW_DOMAINS_ENV_VAR:'["properties"]',
                      tp.LOCAL_DOMAINS_ENV_VAR:'["outliner"]', tp.PROFILE_ENV_VAR:"core"}
        self.assertEqual(tp.resolve_profile(environ=configured).domains, ("properties",))
        self.assertEqual(tp.resolve_profile("all", configured).tool_names, tp.KNOWN_TOOL_NAMES)
        self.assertEqual(tp.resolve_profile(environ={tp.LOCAL_DOMAINS_ENV_VAR:'["global"]'}).domains, ("global",))
        for value in ("", "{}", '["bad"]', '["properties","properties"]', "[1]", "null"):
            with self.assertRaises(tp.ToolProfileValueError):
                tp.resolve_profile(environ={tp.VIEW_DOMAINS_ENV_VAR:value})
        for value in ("", " ", "bad"):
            with self.assertRaises(tp.ToolProfileValueError):
                tp.resolve_profile(value)

    def test_original90_definitions_unchanged(self):
        previous = json.loads((Path(__file__).parent / "fixtures/legacy90_tools.json").read_text(encoding="utf-8"))
        actual = {t.name:t.model_dump(mode="json", by_alias=True, exclude_none=True)
                  for t in asyncio.run(build_server(RecordingBridge()).list_tools())}
        self.assertEqual(set(actual) - set(previous), tp.CONTROL_TOOL_NAMES)
        self.assertEqual(len(previous), 90)
        for name, spec in previous.items():
            self.assertEqual(actual[name], spec, name)

    def test_builder_rejects_unclassified_and_missing(self):
        from blender_mcp.tools import domains
        def extra(registry, client, names):
            @registry.tool()
            def blender_unclassified() -> dict:
                return {}
            names.add("blender_unclassified")
        original = domains.REGISTRATIONS
        missing = tuple(r for r in original if r is not domains.register_audit_tools)
        for profile in ("core", "all"):
            for registrations in (original + (extra,), missing):
                with patch.object(domains, "REGISTRATIONS", registrations):
                    with self.assertRaises(tp.ToolProfileConfigurationError):
                        build_server(RecordingBridge(), profile=profile)

    def test_no_bare_registry_view_configuration(self):
        with self.assertRaises(tp.ToolProfileConfigurationError):
            tp.apply_tool_profile(FastMCP(), "all", tp.KNOWN_TOOL_NAMES)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = RecordingBridge()
        self.server = build_server(self.bridge, profile="global-only")
        self.controller = self.server.domain_controller

    async def names(self):
        return {t.name for t in await self.server.list_tools()}

    async def test_default_all_available_common_directory(self):
        self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)
        state = await self.controller.state()
        self.assertEqual(state["available_tool_count"], 93)
        self.assertEqual(state["view_tool_count"], 25)
        self.assertEqual(state["effective_view_domains"], ["global"])
        self.assertTrue(state["all_tools_active"])
        self.assertFalse(state["client_refresh"]["required_after_change"])
        self.assertFalse(self.bridge.calls)

    async def test_query_every_interface_exact_definitions_without_state_change(self):
        initial = self.controller.selection
        full = {t.name:t for t in await self.server.list_tools()}
        for domain in tp.DOMAINS:
            result = await self.controller.catalog([domain])
            self.assertEqual({t["name"] for t in result["tools"]}, set(tp.DOMAIN_TOOLS[domain]))
            for spec in result["tools"]:
                self.assertEqual(spec, full[spec["name"]].model_dump(mode="json",by_alias=True,exclude_none=True))
            self.assertIs(self.controller.selection, initial)
            self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)
        brief = await self.controller.catalog(["properties"], False)
        self.assertTrue(all("inputSchema" not in t for t in brief["tools"]))
        self.assertFalse(self.bridge.calls)

    async def test_select_add_remove_clear_idempotent_without_notice(self):
        notice = AsyncMock(side_effect=AssertionError("directory changes must not notify"))
        for mode, domains, expected in (
            ("replace", ["geometry_nodes","properties"], {"geometry_nodes","properties"}),
            ("add", ["model_inspection"], {"geometry_nodes","properties","model_inspection"}),
            ("remove", ["geometry_nodes"], {"properties","model_inspection"}),
            ("enable", ["node_frames"], {"properties","model_inspection","node_frames"}),
            ("disable", ["node_frames"], {"properties","model_inspection"}),
            ("replace", [], set()),
        ):
            result = await self.controller.set_domains(mode, domains, notice)
            self.assertEqual(set(result["selected_domains"]), expected)
            self.assertFalse(result["registration_or_call_permissions_changed"])
            self.assertEqual(result["available_tool_count"], 93)
            self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)
        before = self.controller.selection
        repeated = await self.controller.set_domains("replace", [])
        self.assertFalse(repeated["changed"])
        self.assertEqual(repeated["revision"], before.revision)
        notice.assert_not_awaited()

    async def test_business_calls_work_in_other_views_and_after_clear(self):
        for selection in (["outliner"], [], ["properties"]):
            await self.controller.set_domains("replace", selection)
            before = len(self.bridge.calls)
            await self.server.call_tool("blender_list_node_types", {})
            await self.server.call_tool("blender_properties_list_parameters", {"object":"Fixture"})
            self.assertGreaterEqual(len(self.bridge.calls) - before, 2)
            self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)

    async def test_view_selection_does_not_hold_business_execution(self):
        # Browsing and business execution share no permission lock. Check with
        # the real directory lock held by a client querying documentation.
        async with self.controller.lock:
            call = asyncio.create_task(self.server.call_tool("blender_list_node_types", {}))
            await asyncio.wait_for(call, 2)
        self.assertTrue(self.bridge.calls)

    async def test_invalid_view_preserves_state_and_all_availability(self):
        await self.controller.set_domains("replace", ["properties"])
        previous = self.controller.selection
        for mode, domains, code in (
            ("bad", [], "invalid_domain_mode"),
            ("replace", ["bad"], "unknown_domain"),
            ("add", ["properties","properties"], "duplicate_domain"),
            ("replace", ["properties"] * 12, "invalid_domain_selection"),
            ("replace", [1], "invalid_domain_selection"),
        ):
            with self.assertRaisesRegex(ToolError, code):
                await self.controller.set_domains(mode, domains)
            self.assertIs(self.controller.selection, previous)
            self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)
        global_view = await self.controller.set_domains("replace", ["global"])
        self.assertEqual(global_view["view_tool_count"], 25)

    async def test_failed_response_keeps_view(self):
        await self.controller.set_domains("replace", ["properties"])
        before = self.controller.selection
        with patch.object(self.controller, "_state", side_effect=RuntimeError("view render failed")):
            with self.assertRaisesRegex(ToolError, "domain_view_update_failed"):
                await self.controller.set_domains("replace", ["outliner"])
        self.assertIs(self.controller.selection, before)
        self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)

    async def test_actual_registry_checked_before_initialize(self):
        @self.server.registry.tool()
        def blender_unclassified() -> dict:
            return {}
        with patch.object(self.server.protocol, "run", new_callable=AsyncMock) as run:
            with self.assertRaises(tp.ToolProfileConfigurationError):
                await self.server.serve(None, None)
            run.assert_not_awaited()

    async def test_independent_views_same_business_availability(self):
        other = build_server(RecordingBridge())
        await asyncio.gather(self.controller.set_domains("replace", ["properties"]),
                             other.domain_controller.set_domains("replace", ["outliner"]))
        self.assertEqual((await self.controller.state())["selected_domains"], ["properties"])
        self.assertEqual((await other.domain_controller.state())["selected_domains"], ["outliner"])
        self.assertEqual(await self.names(), tp.KNOWN_TOOL_NAMES)
        self.assertEqual({t.name for t in await other.list_tools()}, tp.KNOWN_TOOL_NAMES)
        await other.call_tool("blender_properties_list_parameters", {"object":"Fixture"})

    async def test_query_and_selection_errors_are_audited(self):
        from blender_mcp import audit
        records = []
        log = audit.get_audit()
        original = log.record
        def observe(event, **fields):
            records.append((event, fields))
            return original(event, **fields)
        with patch.object(log, "record", side_effect=observe):
            await self.server.call_tool("blender_tool_domains", {"domains":["properties"]})
            with self.assertRaises(ToolError):
                await self.server.call_tool("blender_set_tool_domains", {"mode":"replace","domains":["bad"]})
        self.assertEqual(sum(e=="tool_start" for e,_ in records), 2)
        self.assertEqual(sum(e=="tool_end" for e,_ in records), 2)


if __name__ == "__main__":
    unittest.main()
