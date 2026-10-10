"""Typed Geometry Nodes modifier *parameter* tools (the Properties panel, via MCP).

Every tool forwards to exactly one ``properties_*`` bridge command; this module
never imports ``bpy`` and never touches the file system.  The signatures declare the
strict shape of the arguments (so the MCP schema refuses an obviously wrong call
before a Blender round trip) while Blender remains the authority for socket types,
ranges, datablock references and writability, and reports stable error codes.

Scope of the domain
-------------------
Only the **per-instance** inputs of one exact object's Geometry Nodes modifier are
read or written (``modifier.properties.inputs[identifier]``).  The shared node group
interface default, another object's instance of the same group, the node graph and
the modifier stack are never touched.  Reading and writing both address a socket by
its stable ``identifier``, never by a localized display name.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ..bridge import BridgeClient
from ._common import call

#: An exact Blender name (object or modifier), matching the rest of this server.
Name = Annotated[str, Field(min_length=1, max_length=255)]
#: A stable node group interface socket identifier (e.g. ``Socket_2``).
Identifier = Annotated[str, Field(min_length=1, max_length=255)]
#: ``VALUE`` selects a single value; ``ATTRIBUTE`` selects the named field.
Mode = Literal["VALUE", "ATTRIBUTE"]

DOMAIN = "properties"


class ParameterChange(BaseModel):
    """One per-instance input change for ``blender_properties_set_parameters``."""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Stable interface socket identifier from blender_properties_list_parameters, "
            "e.g. 'Socket_2'. Display names are never guessed into identifiers."
        ),
    )
    value: Any = Field(
        default=None,
        description=(
            "mode='VALUE': the new value - number, bool, string, a 3/4 element list for "
            "vector/colour/rotation, or {\"id_type\": ..., \"id_name\": ...} for a datablock "
            "input (null clears it). mode='ATTRIBUTE': ignored."
        ),
    )
    mode: Mode = Field(
        default="VALUE",
        description=(
            "VALUE selects the per-instance constant. ATTRIBUTE selects the input's named field; "
            "both modes require the live type selector enum when present."
        ),
    )
    attribute_name: str | None = Field(
        default=None,
        description="mode='ATTRIBUTE' only: the attribute name to read, e.g. 'Position'.",
    )


#: One batch call carries at most this many changes (mirrored on the Blender side).
Changes = Annotated[list[ParameterChange], Field(min_length=1, max_length=128)]


def _meta(mutating: bool, idempotent: bool) -> dict:
    """The ``_meta`` block of every Properties tool (domain marker + contract flags)."""
    return {
        "domain": DOMAIN,
        "bridge_prefix": "properties_",
        "mutating": mutating,
        "idempotent": idempotent,
        "deletes_data": False,
        "writes_shared_node_group_default": False,
    }


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_properties_list_parameters",
        title="Properties: Geometry Nodes modifier parameters",
        description=(
            "Enumerate the parameters a Geometry Nodes modifier exposes on ONE object - the same "
            "inputs the Properties > Modifier panel shows. Locate the modifier by exact object name "
            "and exact modifier name (modifier may be omitted only when the object has exactly one "
            "Geometry Nodes modifier; several are refused as ambiguous). For every interface input "
            "this returns the stable socket identifier, display name, socket type, the shared "
            "node-group interface default (marked default_is_shared=true), the current per-instance "
            "value, the declared min/max, subtype and unit, the hidden flags (hide_value / "
            "hide_in_modifier), whether the socket is linked inside the group, whether it is a field "
            "socket, the proven attribute-mode state with the raw IDPropertyGroup keys, the reliably "
            "discoverable enum items for menu sockets, any F-Curve driver that would overwrite a "
            "write, and whether this server may write it (writable / readonly_reason). Read-only: "
            "nothing in the scene, the node group or the file is modified."
        ),
        annotations=ToolAnnotations(
            title="Properties: Geometry Nodes modifier parameters",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(False, True),
    )
    def blender_properties_list_parameters(
        object: Name,
        modifier: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
    ) -> dict:
        return call(bridge, "properties_list_parameters",
                    {"object": object, "modifier": modifier})

    @mcp.tool(
        name="blender_properties_get_parameter",
        title="Properties: read one modifier parameter",
        description=(
            "Read ONE Geometry Nodes modifier input of one object by its stable socket identifier "
            "(never by a localized display name, and never by guessing when names collide). Returns "
            "the current per-instance value, the shared interface default, the declared range, the "
            "subtype/unit, the hidden and link state, the proven attribute-mode state, the enum "
            "items of a menu socket and the writability verdict with its exact reason. Read-only."
        ),
        annotations=ToolAnnotations(
            title="Properties: read one modifier parameter",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(False, True),
    )
    def blender_properties_get_parameter(
        object: Name,
        identifier: Identifier,
        modifier: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
    ) -> dict:
        return call(bridge, "properties_get_parameter",
                    {"object": object, "modifier": modifier, "identifier": identifier})

    @mcp.tool(
        name="blender_properties_set_parameter",
        title="Properties: set one modifier parameter",
        description=(
            "Set ONE per-instance Geometry Nodes modifier input of one object and verify the write by "
            "reading it back. Only modifier.properties.inputs[identifier] of that modifier is written: "
            "the shared node group interface default and every other object's instance stay untouched. "
            "mode='VALUE' writes a number, bool, string, a 3 element vector/rotation or 4 element "
            "colour list, or a datablock reference {\"id_type\": \"OBJECT\", \"id_name\": \"Cube\"} "
            "(null clears a datablock input); values are checked against the socket type and its "
            "declared range before the write. mode='ATTRIBUTE' writes attribute_name AND the "
            "verified type selector; unsupported modes are refused. Menu integers are RNA enum "
            "values, never assumed to be positional indices; an identifier maps to its live value. "
            "Refused before writing: linked/overridden data (read_only_linked), an F-Curve driver on "
            "the input (read_only_driven), hidden inputs (hidden_input), geometry/bundle sockets "
            "(read_only_socket_type / unsupported_socket_type), menu values whose representation is "
            "unproven (unsupported_enum), unknown identifiers (input_not_found) and output sockets "
            "(not_an_input). After writing, the object is tagged and the dependency graph is "
            "re-evaluated; the response reports the parameter diff and the real evaluation state, or "
            "the exact step that failed instead of claiming a synchronous update."
        ),
        annotations=ToolAnnotations(
            title="Properties: set one modifier parameter",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        meta=_meta(True, False),
    )
    def blender_properties_set_parameter(
        object: Name,
        identifier: Identifier,
        value: Any = None,
        modifier: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
        mode: Mode = "VALUE",
        attribute_name: str | None = None,
    ) -> dict:
        return call(
            bridge,
            "properties_set_parameter",
            {
                "object": object,
                "modifier": modifier,
                "identifier": identifier,
                "value": value,
                "mode": mode,
                "attribute_name": attribute_name,
            },
        )

    @mcp.tool(
        name="blender_properties_set_parameters",
        title="Properties: set several modifier parameters atomically",
        description=(
            "Set up to 128 per-instance Geometry Nodes modifier inputs of ONE object in a single call. "
            "The whole batch is pre-flighted first - every identifier, socket type, value, range, "
            "datablock reference, mode and writability - and only then written, so a refused batch "
            "leaves every value and mode exactly as it was (batch_precheck_failed lists every refused "
            "change with its own code). A repeated identifier in one batch is refused "
            "(duplicate_identifier). If a write or the read-back verification fails, every touched "
            "input is restored from a snapshot of all its keys and the error reports the failures, the "
            "restored keys and any key that could not be restored - a half-applied batch is never "
            "reported as a success. The response contains a structured before/after diff per input, "
            "the written keys, and the dependency-graph evaluation state after the writes."
        ),
        annotations=ToolAnnotations(
            title="Properties: set several modifier parameters atomically",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        meta=_meta(True, False),
    )
    def blender_properties_set_parameters(
        object: Name,
        changes: Changes,
        modifier: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
    ) -> dict:
        return call(
            bridge,
            "properties_set_parameters",
            {
                "object": object,
                "modifier": modifier,
                "changes": [change.model_dump(exclude_none=True) for change in changes],
            },
        )
