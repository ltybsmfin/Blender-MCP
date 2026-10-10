"""Interface-specific tool-directory browsing over an always-complete registry.

Uses public mcp==1.30.0 handlers. Views are session-local discovery data;
they never filter tools/list or gate tools/call.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

import anyio
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent

from . import tool_profiles as tp


DOMAIN_DETAILS = {
    "outliner": ("Outliner 集合与对象", "层级、成员、创建删除、重命名及显示开关", ()),
    "object_modeling": ("对象建模", "网格、基本体与自定义属性", ("geometry_nodes",)),
    "geometry_nodes": ("Geometry Nodes", "节点、接口、修改器、连线、Reroute、布局及编辑器截图", ("properties", "model_inspection", "node_frames")),
    "properties": ("Properties 实例参数", "参数查询、写入与包含求值/视口的复合迭代", ("geometry_nodes", "model_inspection")),
    "model_inspection": ("模型检查", "依赖图求值与真实3D视口截图", ("properties",)),
    "node_frames": ("节点 Frame", "原生画布分组、布局与 Text 注释", ("geometry_nodes",)),
    "node_notes": ("节点注释扩展", "可选 Mod 扩展；内部使用原生 Frame/Text", ("geometry_nodes",)),
    "viewport": ("3D Viewport 与预览", "明确选择/取景/置前和渲染预览", ("model_inspection",)),
    "shape_keys": ("形态键", "原始网格形态键查询、创建、修改与删除", ("object_modeling",)),
    "drivers": ("驱动器", "明确数据路径的驱动器查询、创建、修改与删除", ("properties", "shape_keys")),
}


def domain_error(code: str, message: str, **details):
    error = ToolError(json.dumps({"code": code, "message": message, **details}, ensure_ascii=False))
    error.code = code
    return error


def error_mapping(error):
    """Recover our typed error through FastMCP's public ToolError cause chain."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "code", None):
            try:
                value = json.loads(str(error))
                if isinstance(value, dict) and value.get("code") == error.code:
                    return value
            except (ValueError, TypeError):
                pass
        error = error.__cause__ or error.__context__
    return None


@dataclass(frozen=True)
class Selection:
    domains: tuple[str, ...]
    revision: int = 0
    source: str = "default"


class DomainController:
    """Session-local directory view; never controls registration or invocation."""

    def __init__(self, registry, registered_names):
        self.registry = registry
        self.registered_names = tp.validate_registered(registered_names)
        if set(DOMAIN_DETAILS) != set(tp.LOCAL_DOMAINS):
            raise tp.ToolProfileConfigurationError("local domain catalog is incomplete")
        self.lock = asyncio.Lock()
        self.started = False
        self.initial_profile = tp.DEFAULT_PROFILE
        self.initial_source = "default"
        self.selection = Selection(())

    def configure_initial(self, profile, source):
        if self.started:
            raise tp.ToolProfileConfigurationError("cannot change startup view after serving; use the directory tool")
        if profile.tool_names != tp.KNOWN_TOOL_NAMES:
            raise tp.ToolProfileConfigurationError("all registered tools must stay available")
        self.initial_profile = profile.name
        self.initial_source = source
        self.selection = Selection(profile.domains, source=source)

    async def _checked_tools(self):
        tools = await self.registry.list_tools()
        tp.validate_registered(t.name for t in tools)
        return tools

    def _validate_domains(self, domains):
        if not isinstance(domains, list) or len(domains) > len(tp.DOMAINS):
            raise domain_error("invalid_domain_selection", f"domains must be an array of at most {len(tp.DOMAINS)} IDs")
        if any(not isinstance(d, str) or not d for d in domains):
            raise domain_error("invalid_domain_selection", "domain IDs must be non-empty strings")
        if len(domains) != len(set(domains)):
            raise domain_error("duplicate_domain", "duplicate domain IDs are rejected")
        unknown = sorted(set(domains) - set(tp.DOMAINS))
        if unknown:
            raise domain_error("unknown_domain", "unknown tool-directory domain", domains=unknown)
        return tuple(d for d in tp.DOMAINS if d in domains)

    def _definitions(self, tools, domains, include_parameters):
        names = tp.view_tool_names_for_domains(domains)
        result = []
        for tool in tools:
            if tool.name not in names:
                continue
            definition = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            if not include_parameters:
                definition.pop("inputSchema", None)
                definition.pop("outputSchema", None)
            result.append(definition)
        if {t["name"] for t in result} != set(names):
            raise tp.ToolProfileConfigurationError("incomplete directory view")
        return result

    def _state(self, tools, selection=None, include_parameters=False):
        current = selection or self.selection
        definitions = self._definitions(tools, current.domains, include_parameters)
        return {
            "semantics": "directory_view_only_all_tools_always_available",
            "selected_domains": list(current.domains),
            "effective_view_domains": list(current.domains) or ["global"],
            "view_tool_names": [t["name"] for t in definitions],
            "view_tool_count": len(definitions), "tools": definitions,
            "available_tool_count": len(tools), "all_tools_active": True,
            "registration_or_call_permissions_changed": False,
            "revision": current.revision, "initial_profile": self.initial_profile,
            "configuration_source": current.source,
            "initial_configuration_source": self.initial_source,
            "scope": "directory_view_per_stdio_client_session",
            "notification_sent": False,
            "client_refresh": {"required_after_change": False,
                               "action": "Read the returned directory. All tools remain registered and callable; no reconnect is needed to browse another domain."},
        }

    async def list_tools(self):
        # The protocol list is always complete, independent of the directory.
        return await self._checked_tools()

    async def state(self, include_parameters=False):
        async with self.lock:
            return self._state(await self._checked_tools(), include_parameters=include_parameters)

    async def catalog(self, domains=None, include_parameters=True):
        async with self.lock:
            tools = await self._checked_tools()
            if domains is not None:
                selected = self._validate_domains(domains)
                definitions = self._definitions(tools, selected, include_parameters)
                return {"semantics": "directory_query_only",
                        "requested_domains": list(selected),
                        "effective_view_domains": list(selected) or ["global"],
                        "tools": definitions, "tool_count": len(definitions),
                        "available_tool_count": len(tools), "all_tools_active": True,
                        "selection_changed": False}
            entries = []
            for domain in tp.DOMAINS:
                detail = ("通用工具", "诊断、工程、场景、日志与工具目录", ()) if domain == "global" else DOMAIN_DETAILS[domain]
                entries.append({"id": domain, "name": detail[0], "description": detail[1],
                                "tools": list(tp.DOMAIN_TOOLS[domain]),
                                "tool_count": len(tp.DOMAIN_TOOLS[domain]),
                                "suggested_domains": list(detail[2]),
                                "selected": domain in (self.selection.domains or ("global",)),
                                "all_tools_active": True})
            return {"semantics": "directory_overview_only",
                    "domains": entries, "available_tool_count": len(tools),
                    "all_tools_active": True, "selection_changed": False}

    async def set_domains(self, mode, domains, notify=None):
        # enable/disable are migration aliases for ADD/REMOVE DIRECTORY entries.
        # They never enable or disable a business tool. No session/notice needed.
        mode = {"enable": "add", "disable": "remove"}.get(mode, mode)
        if mode not in ("replace", "add", "remove"):
            raise domain_error("invalid_domain_mode", "mode must be replace, add or remove (enable/disable are legacy view aliases)")
        requested = self._validate_domains(domains)
        async with self.lock:
            previous = self.selection
            tools = await self._checked_tools()
            selected = set(previous.domains)
            selected = set(requested) if mode == "replace" else (
                selected | set(requested) if mode == "add" else selected - set(requested))
            ordered = tuple(d for d in tp.DOMAINS if d in selected)
            changed = ordered != previous.domains
            candidate = Selection(ordered, previous.revision + int(changed),
                                  "runtime_explicit_directory_view" if changed else previous.source)
            try:
                # Build and validate the result before committing any view state.
                response = self._state(tools, candidate, include_parameters=True)
            except Exception as exc:
                raise domain_error("domain_view_update_failed", "directory selection unchanged",
                                   committed=False, rolled_back=True, reason=str(exc)) from exc
            self.selection = candidate
            return {**response, "changed": changed, "committed": True}


class DynamicDomainServer:
    """Public low-level MCP handlers around unchanged audited FastMCP tools."""
    def __init__(self, registry, controller, name, instructions):
        self.registry = registry
        self.instructions = instructions
        self.domain_controller = controller
        self.protocol = Server(name, instructions=instructions)
        self._serving = False  # our own guard; never an SDK private field
        self._served_once = False
        self.protocol.list_tools()(self.list_tools)
        self.protocol.call_tool(validate_input=False)(self.protocol_call_tool)
        # Preserve FastMCP's empty resources/prompts surfaces via public handlers.
        self.protocol.list_resources()(registry.list_resources)
        self.protocol.list_resource_templates()(registry.list_resource_templates)
        self.protocol.read_resource()(registry.read_resource)
        self.protocol.list_prompts()(registry.list_prompts)
        self.protocol.get_prompt()(registry.get_prompt)

    async def list_tools(self):
        return await self.domain_controller.list_tools()

    async def call_tool(self, name, arguments):
        return await self.registry.call_tool(name, arguments)

    async def protocol_call_tool(self, name, arguments):
        try:
            return await self.call_tool(name, arguments)
        except ToolError as exc:
            mapped = error_mapping(exc)
            if mapped is None:
                raise
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(mapped, ensure_ascii=False))],
                                  structuredContent=mapped, isError=True)
    async def serve(self, read_stream, write_stream):
        if self._serving or self._served_once:
            raise RuntimeError("one stdio server object serves one client session; create a separate process")
        self._serving = True
        self._served_once = True
        self.domain_controller.started = True
        try:
            # Validate the real public SDK registry before initialize. This also
            # catches registrations that bypassed the domain registrar's name
            # bookkeeping, instead of waiting for the first tools/list request.
            await self.domain_controller._checked_tools()
            options = self.protocol.create_initialization_options(NotificationOptions(tools_changed=False))
            await self.protocol.run(read_stream, write_stream, options)
        finally:
            self._serving = False

    async def run_stdio_async(self):
        async with stdio_server() as (read, write):
            await self.serve(read, write)

    def run(self, transport="stdio"):
        if transport != "stdio":
            raise ValueError("tool-directory service supports one-client stdio only")
        anyio.run(self.run_stdio_async)


def register_control_tools(registry, controller):
    @registry.tool()
    async def blender_tool_domains(domains: list[str] | None = None, include_parameters: bool = True) -> dict:
        """Browse tools by Blender interface. Omit domains for directory overview; pass domain IDs for just their tool descriptions and optional input/output schemas. Query only: every registered tool remains available."""
        return await controller.catalog(domains, include_parameters)

    @registry.tool()
    async def blender_tool_domain_state(include_parameters: bool = False) -> dict:
        """Read the selected tool-directory view and its tools. All business tools remain registered and callable regardless of this view. No Blender connection or client refresh needed."""
        return await controller.state(include_parameters)

    @registry.tool()
    async def blender_set_tool_domains(mode: Literal["replace", "add", "remove", "enable", "disable"], domains: list[str]) -> dict:
        """Select which Blender interface tool directory to read; returns its tool descriptions and parameter schemas. replace/add/remove change only the directory view; [] restores the common-tool view. enable/disable are legacy aliases for adding/removing directory entries, never tool activation. All tools stay callable; no list-change notification or reconnect."""
        return await controller.set_domains(mode, domains)
