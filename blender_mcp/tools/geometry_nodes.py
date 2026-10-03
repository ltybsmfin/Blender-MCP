"""Geometry Nodes tools: the primary workflow of this MCP server.

Recommended order (the same order is spelled out in the server instructions):

1. ``blender_inspect_node_group`` on an existing group, or
   ``blender_list_node_types`` / ``blender_describe_node_type`` for runtime
   discovery of what this Blender build actually offers.
2. ``blender_create_node_group`` with a typed interface (name, socket type,
   defaults, ranges).
3. ``blender_create_node`` + ``blender_set_socket_default`` + ``blender_link_sockets``,
   preferably through ``blender_batch_graph_edit``.
4. ``blender_attach_geometry_nodes_modifier`` and then drive the exposed
   parameters with ``blender_set_modifier_input``.
5. Verify with ``blender_object_info`` (evaluated mesh counts/bounds) instead of
   assuming that a parameter took effect.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..bridge import BridgeClient
from ..models import GraphOperation, InterfaceSocketSpec
from ._common import call


def _rollback_message(group: str, payload: dict) -> str:
    """A bounded, explicit description of a transaction that was rolled back."""
    failures = payload.get("failures") or []
    head = failures[0] if failures else {}
    detail = "; ".join(
        "#%s %s: %s (%s)" % (entry.get("index"), entry.get("op"), entry.get("error"), entry.get("code"))
        for entry in failures[:8]
        if isinstance(entry, dict)
    )
    extra = len(failures) - 8
    if extra > 0:
        detail += "; +%d more" % extra
    return (
        "graph_edit_rolled_back: node group %r was NOT modified - operation #%s (%s) failed, so the whole "
        "transaction was rolled back to the original group: %s. Nothing is half-applied: nodes, links, "
        "socket defaults, interface, modifier inputs and references are unchanged. Fix the failing "
        "operation and retry, or pass transactional=false to apply the batch non-atomically."
        % (
            payload.get("group") or group,
            head.get("index"),
            head.get("op"),
            detail or "no failure detail was reported",
        )
    )


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_create_node_group",
        description=(
            "Create a GeometryNodeTree node group with a fully typed interface. Each interface entry "
            "becomes a real group socket with identifier, default value and UI range, which is what the "
            "Geometry Nodes modifier later exposes as parameters. By default the Group Input and Group "
            "Output nodes are created and a 'Geometry' output socket is added if you did not declare one. "
            "Datablock sockets (NodeSocketObject/Material/Collection/Image) take a structured reference as "
            "their default_value, e.g. {\"id_type\": \"OBJECT\", \"id_name\": \"Cube.001\"}, or null to "
            "leave them empty; other reference kinds (NODETREE/SCENE) are not valid interface defaults."
        ),
    )
    def blender_create_node_group(
        name: str,
        interface: list[InterfaceSocketSpec] | None = None,
        with_input_output: bool = True,
        geometry_output: bool = True,
        if_exists: Literal["error", "replace", "reuse"] = "error",
    ) -> dict:
        return call(
            bridge,
            "create_node_group",
            {
                "name": name,
                "interface": [spec.model_dump() for spec in (interface or [])],
                "with_input_output": with_input_output,
                "geometry_output": geometry_output,
                "if_exists": if_exists,
            },
        )

    @mcp.tool(
        name="blender_create_node",
        description=(
            "Add one node to a node group. node_type is a Blender node idname such as 'GeometryNodeTransform', "
            "'GeometryNodeMeshGrid', 'GeometryNodeSetPosition', 'ShaderNodeCombineXYZ' or 'GeometryNodeGroup'. "
            "If the node is created but 'settings' is rejected the node is removed again, so a failed call "
            "leaves no half-configured node behind. Always inspect the returned node payload for the REAL "
            "socket identifiers - display names are localized and can change between Blender versions."
        ),
    )
    def blender_create_node(
        group: str,
        node_type: str,
        name: str | None = None,
        location: list[float] | None = None,
        label: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict:
        return call(
            bridge,
            "create_node",
            {
                "group": group,
                "node_type": node_type,
                "name": name,
                "location": location,
                "label": label,
                "settings": settings,
            },
        )

    @mcp.tool(
        name="blender_set_node_property",
        description=(
            "Set one RNA property on a node (for example 'operation', 'domain', 'data_type', 'mode', or the "
            "'node_tree' pointer of a GeometryNodeGroup). Property names are the RNA identifiers reported by "
            "blender_describe_node_type; enum values are validated by Blender and the allowed identifiers are "
            "returned on failure. Datablock-pointer properties take a structured reference instead of a "
            "string: {\"id_type\": \"NODETREE\", \"id_name\": \"MyGroup\"} (reference kinds: OBJECT, "
            "NODETREE, MATERIAL, COLLECTION, IMAGE), and null clears the pointer. The referenced datablock "
            "must already exist and must match the property's RNA fixed type - a GeometryNodeGroup also "
            "refuses a ShaderNodeTree, which Blender itself would accept. On failure the old pointer is left "
            "untouched. Assigning a group pointer rebuilds the node's sockets, so links are dropped by "
            "Blender: the response reports links_before/links_after plus a warning, and links must be made "
            "again afterwards (see blender_inspect_node_group for the new identifiers)."
        ),
    )
    def blender_set_node_property(group: str, node: str | int, property: str, value: Any) -> dict:
        return call(
            bridge,
            "set_node_property",
            {"group": group, "node": node, "property": property, "value": value},
        )

    @mcp.tool(
        name="blender_set_socket_default",
        description=(
            "Set a node socket's default value. The socket may be addressed by identifier (preferred), "
            "display name or index. Values: number, bool, string, or a list for vector/colour/rotation "
            "sockets (a 3-element list is accepted for a rotation socket and converted to a quaternion). "
            "A default value on a socket that is already linked has no effect until you unlink it. "
            "Datablock sockets (Object Info's 'Object', Set Material's 'Material', Collection Info's "
            "'Collection', Image Texture's 'Image', ...) take a structured reference, e.g. "
            "{\"id_type\": \"OBJECT\", \"id_name\": \"Cube\"} with reference kinds OBJECT, NODETREE, "
            "MATERIAL, COLLECTION or IMAGE; null clears the pointer back to empty. A plain string or number "
            "is refused for those sockets, a wrong-type or missing datablock is refused with an explicit "
            "error, and the previous pointer is left untouched. The readback serialises the pointer as a "
            "reference object so you can see exactly what is assigned."
        ),
    )
    def blender_set_socket_default(
        group: str,
        node: str | int,
        socket: str | int,
        value: Any,
        in_out: Literal["INPUT", "OUTPUT"] = "INPUT",
    ) -> dict:
        return call(
            bridge,
            "set_socket_default",
            {"group": group, "node": node, "socket": socket, "value": value, "in_out": in_out},
        )

    @mcp.tool(
        name="blender_link_sockets",
        description=(
            "Link an output socket to an input socket. Identifiers come from blender_inspect_node_group or "
            "blender_describe_node_type. Type compatibility is checked before Blender is asked, so an "
            "impossible link (for example Geometry into a Float) is an explicit error. Linking into an "
            "already linked input replaces the old link unless replace=false."
        ),
    )
    def blender_link_sockets(
        group: str,
        from_node: str | int,
        from_socket: str | int,
        to_node: str | int,
        to_socket: str | int,
        replace: bool = True,
    ) -> dict:
        return call(
            bridge,
            "link_sockets",
            {
                "group": group,
                "from_node": from_node,
                "from_socket": from_socket,
                "to_node": to_node,
                "to_socket": to_socket,
                "replace": replace,
            },
        )

    @mcp.tool(
        name="blender_unlink_socket",
        description=(
            "Remove every link that touches one socket and report exactly which links were removed. Use it "
            "before setting a default value on a socket that is currently driven by a link."
        ),
    )
    def blender_unlink_socket(
        group: str,
        node: str | int,
        socket: str | int,
        in_out: Literal["INPUT", "OUTPUT"] = "INPUT",
    ) -> dict:
        return call(
            bridge,
            "unlink_socket",
            {"group": group, "node": node, "socket": socket, "in_out": in_out},
        )

    @mcp.tool(
        name="blender_remove_node",
        description=(
            "Remove one node from a node group and report how many links disappeared with it. The Group "
            "Input / Group Output interface nodes are protected and cannot be removed this way."
        ),
    )
    def blender_remove_node(group: str, node: str | int) -> dict:
        return call(bridge, "remove_node", {"group": group, "node": node})

    @mcp.tool(
        name="blender_inspect_node_group",
        description=(
            "Full structural report of a node group: every node with its index, idname, label, location, "
            "inputs/outputs with identifiers, types, default values and link state; every link with "
            "identifiers on both ends; the interface (inputs/outputs with identifiers, defaults, ranges, "
            "subtypes, hidden flags); which objects use the group as a modifier; and drivers on the group. "
            "Call this BEFORE linking anything - socket identifiers are the stable handles."
        ),
    )
    def blender_inspect_node_group(name: str, include_sockets: bool = True) -> dict:
        return call(bridge, "inspect_node_group", {"name": name, "include_sockets": include_sockets})

    @mcp.tool(
        name="blender_attach_geometry_nodes_modifier",
        description=(
            "Attach a node group to a mesh object as a Geometry Nodes modifier and optionally set its "
            "exposed parameters in the same call. Existing modifiers are preserved (this is additive and "
            "procedural), the modifier is re-used when it is already attached to the same group, and the "
            "response contains the evaluated mesh counts/bounds so you can see the effect immediately."
        ),
    )
    def blender_attach_geometry_nodes_modifier(
        object: str,
        group: str,
        modifier_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        reuse_existing: bool = True,
    ) -> dict:
        return call(
            bridge,
            "attach_geometry_nodes_modifier",
            {
                "object": object,
                "group": group,
                "modifier_name": modifier_name,
                "parameters": parameters,
                "reuse_existing": reuse_existing,
            },
        )

    @mcp.tool(
        name="blender_set_modifier_input",
        description=(
            "Set one exposed Geometry Nodes parameter on a mesh object's modifier and read every parameter "
            "back. Values must match the interface socket type (float, int, bool, string, or a vector list). "
            "Datablock parameters (Object/Material/Collection/Image inputs) take a structured reference such "
            "as {\"id_type\": \"OBJECT\", \"id_name\": \"Cube\"} and null clears them; plain values are "
            "refused there on purpose, because Blender's ID-property store would silently keep a string that "
            "names no datablock. Geometry inputs cannot be set by value. With include_evaluated=true the "
            "response also contains the re-evaluated mesh counts, world bounds and a mesh signature, which "
            "is the reliable way to prove a parameter changed geometry."
        ),
    )
    def blender_set_modifier_input(
        object: str,
        input: str,
        value: Any,
        modifier: str | None = None,
        include_evaluated: bool = True,
    ) -> dict:
        return call(
            bridge,
            "set_modifier_input",
            {
                "object": object,
                "input": input,
                "value": value,
                "modifier": modifier,
                "include_evaluated": include_evaluated,
            },
        )

    @mcp.tool(
        name="blender_list_node_types",
        description=(
            "Runtime discovery of creatable node types in THIS Blender build (query the live bpy.types "
            "registry, not a hardcoded list). Filter with 'prefix' ('GeometryNode', 'ShaderNode', "
            "FunctionNode', 'CompositorNode') and an optional case-insensitive 'contains' substring, e.g. "
            "contains='Mesh' or contains='Set'. Use it when you are unsure whether a node exists in this "
            "version."
        ),
    )
    def blender_list_node_types(
        prefix: str = "GeometryNode",
        contains: str | None = None,
        limit: int = 200,
    ) -> dict:
        return call(bridge, "list_node_types", {"prefix": prefix, "contains": contains, "limit": limit})

    @mcp.tool(
        name="blender_describe_node_type",
        description=(
            "Instantiate a throwaway node of the given type and report its ACTUAL sockets (name, "
            "identifier, index, type, default value) plus its RNA properties with types, defaults, "
            "ranges and enum identifiers. This is the compatibility tool: it tells you exactly what this "
            "Blender version expects before you build a graph."
        ),
    )
    def blender_describe_node_type(node_type: str) -> dict:
        return call(bridge, "describe_node_type", {"node_type": node_type})

    @mcp.tool(
        name="blender_batch_graph_edit",
        description=(
            "Apply an ordered list of graph edits (create_node, set_node_property, set_socket_default, "
            "link_sockets, unlink_socket, remove_node) in ONE call and get per-operation results. "
            "TRANSACTIONAL BY DEFAULT: the whole list is applied to an isolated staging copy of the node "
            "group and the group is only swapped in when every operation succeeded. A failure therefore "
            "leaves the original group byte-for-byte intact - nodes, links, socket defaults, the interface, "
            "modifier input values, nested group references and drivers - and comes back as a tool error "
            "with status 'rolled_back' plus the per-step failures; nothing is half-applied. On success the "
            "response reports committed=true and the group datablock identity is replaced (same name and "
            "interface identifiers, so users keep working). Linked-library groups are refused. Set "
            "transactional=false to get the legacy non-atomic behaviour, which is the ONLY mode in which "
            "continue_on_error is accepted; in that mode 'completed' edits stay applied and must be "
            "repaired explicitly (the response then reports rollback=false, partial=true)."
        ),
    )
    def blender_batch_graph_edit(
        group: str,
        operations: list[GraphOperation],
        transactional: bool = True,
        continue_on_error: bool = False,
    ) -> dict:
        payload = call(
            bridge,
            "batch_graph_edit",
            {
                "group": group,
                "operations": [operation.to_bridge_params() | {"op": operation.op} for operation in operations],
                "transactional": transactional,
                "continue_on_error": continue_on_error,
            },
        )
        if payload.get("rolled_back"):
            error = ToolError(_rollback_message(group, payload))
            # The audit layer reads this to log the per-step results and the
            # rollback verdict even though the call is reported as an error.
            error.audit_payload = payload  # type: ignore[attr-defined]
            raise error
        return payload
