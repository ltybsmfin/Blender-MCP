"""Geometry Nodes canvas layout and Reroute tools (MCP side).

Five bounded, named tools that talk to the ``geometry_nodes_*`` bridge commands
implemented in ``MCP/blender/geometry_nodes_edit.py``:

* ``blender_geometry_nodes_layout`` - machine-readable picture of one exact
  ``GeometryNodeTree``: node type/name/label, canvas-local and absolute
  coordinates, parent Frame, width/dimensions plus a reliability flag, sockets
  (identifier, type, link state, multi-input flag), the group interface, every
  link endpoint with its ``multi_input_sort_id`` and position in the socket,
  Frames, Reroutes and every object/modifier that uses the tree.
* ``blender_geometry_nodes_move_nodes`` - move explicitly named nodes. Nothing
  else on the canvas is touched.
* ``blender_geometry_nodes_reroute_insert`` - insert native ``NodeReroute``
  nodes into links that already exist: one reroute per link, or one shared
  branch reroute for a single source that fans out to several targets. Links
  from different sources into one multi-input socket always get independent
  reroutes and keep their order.
* ``blender_geometry_nodes_reroute_move`` - move reroutes only.
* ``blender_geometry_nodes_reroute_delete`` - delete named reroutes and
  reconnect each one's source to all of its downstream targets.

Every mutating tool returns ``before``/``after`` plus a ``validation`` block
proving that the *resolved* data flow (reroutes ignored) and the multi-input
order are unchanged, and the bridge rolls the whole edit back on any failure.
No optional add-on is involved, and no tool here saves the .blend file.
"""

from __future__ import annotations

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP

from ..bridge import BridgeClient
from ._common import call


class NodePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(min_length=1, max_length=255)
    x: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)
    y: float = Field(allow_inf_nan=False, ge=-1e6, le=1e6)


class ExistingLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_node: str = Field(min_length=1, max_length=255)
    from_socket: str = Field(min_length=1, max_length=255)
    to_node: str = Field(min_length=1, max_length=255)
    to_socket: str = Field(min_length=1, max_length=255)
    multi_input_sort_id: int | None = Field(default=None, ge=0)


Positions = Annotated[list[NodePosition], Field(min_length=1, max_length=128)]
Links = Annotated[list[ExistingLink], Field(min_length=1, max_length=128)]

#: Shared vocabulary for the two move tools.
PositionsDoc = (
    "List of {'node': exact node name, 'x': number, 'y': number}. With absolute=false (the default) "
    "the values are the node's canvas-local location, exactly like the location field reported by "
    "blender_geometry_nodes_layout; with absolute=true they are canvas coordinates and the parent "
    "Frame offset is subtracted for you. Duplicate, unknown or non-finite entries are refused before "
    "anything is written, and nothing outside this list is moved."
)

RerouteLinksDoc = (
    "List of existing links to interrupt, each {'from_node', 'from_socket', 'to_node', 'to_socket'} "
    "using the identifiers reported by blender_geometry_nodes_layout, plus the optional "
    "'multi_input_sort_id' assertion for links into a multi-input socket (the link is then only "
    "accepted when its current sort id matches). Every link must exist exactly once; missing, "
    "duplicated or ambiguous links are refused before anything is written. At most 128 links."
)


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_geometry_nodes_layout",
        description=(
            "Read-only structure of one exact Geometry Nodes node group (GeometryNodeTree only). "
            "Returns every node with bl_idname, label, canvas-local and absolute location, parent Frame, "
            "width/dimensions plus layout_metrics_reliable (false while a node editor has not drawn the "
            "tree), sockets with identifier/type/link state/is_multi_input, the group interface "
            "(identifier, socket type, defaults, hide flags), every link with both endpoints, its "
            "is_valid/is_muted state, multi_input_sort_id and 0-based multi_input_order, the Frames with "
            "their children, the Reroutes with in/out counts, the ordered resolved sources of every "
            "multi-input socket, and every object/modifier that uses the tree. Pass object (and modifier "
            "when the object has several Geometry Nodes modifiers) to prove that the group belongs to that "
            "modifier: a mismatch is an error, never a guess. This tool never writes and never rearranges."
        ),
    )
    def blender_geometry_nodes_layout(group: str, object: str | None = None, modifier: str | None = None) -> dict:
        return call(bridge, "geometry_nodes_layout", {"group": group, "object": object, "modifier": modifier})

    @mcp.tool(
        name="blender_geometry_nodes_move_nodes",
        description=(
            "Move explicitly named nodes of one exact GeometryNodeTree to the requested canvas "
            "coordinates. Only the listed nodes are written - no neighbour, Frame or reroute is "
            "repositioned, no name is changed and no link is touched. " + PositionsDoc + " "
            "On any failure the original locations are restored from a snapshot of the same tree and the "
            "call is reported as an error with rolled_back; on success the response contains before/after "
            "positions and a validation block."
        ),
    )
    def blender_geometry_nodes_move_nodes(
        group: str,
        positions: Positions,
        absolute: bool = False,
    ) -> dict:
        return call(
            bridge,
            "geometry_nodes_move_nodes",
            {"group": group, "positions": [p.model_dump() for p in positions], "absolute": absolute},
        )

    @mcp.tool(
        name="blender_geometry_nodes_reroute_insert",
        description=(
            "Insert Blender's native NodeReroute nodes into links that ALREADY exist, the structured "
            "equivalent of Ctrl/Shift + right-click on a wire - no mouse gesture and no geometry change. "
            + RerouteLinksDoc + " "
            "shared_branch=false inserts one reroute per listed link. shared_branch=true inserts one "
            "reroute per distinct source socket and fans it out to every listed target of that source, "
            "which is how a single output feeding several inputs is collected into one branch anchor; "
            "links from DIFFERENT sources into the same multi-input socket still get independent reroutes "
            "and never merge. points optionally gives one [x, y] canvas position per created reroute "
            "(the count must match exactly); without points each reroute lands on the midpoint between its "
            "source and its target node. The operation saves the target links, the full multi-input order "
            "and the node positions first, verifies afterwards that the resolved data flow and the "
            "multi-input order are unchanged, tags the group and its users for re-evaluation, and rolls "
            "the whole edit back on any failure (reported as rolled_back). Reroutes are canvas anchors: "
            "they reduce crossings and long wires, they do not make wiring orthogonal."
        ),
    )
    def blender_geometry_nodes_reroute_insert(
        group: str,
        links: Links,
        points: list[list[float]] | None = None,
        shared_branch: bool = False,
    ) -> dict:
        return call(
            bridge,
            "geometry_nodes_reroute_insert",
            {"group": group, "links": [link.model_dump(exclude_none=True) for link in links], "points": points, "shared_branch": shared_branch},
        )

    @mcp.tool(
        name="blender_geometry_nodes_reroute_move",
        description=(
            "Move explicitly named NodeReroute nodes of one exact GeometryNodeTree. Any name that is not "
            "an existing reroute is refused with wrong_node_type, so this can never move a real node by "
            "accident. " + PositionsDoc + " Fails atomic: the original reroute locations are restored and "
            "the call is reported as an error when anything goes wrong."
        ),
    )
    def blender_geometry_nodes_reroute_move(
        group: str,
        positions: Positions,
        absolute: bool = False,
    ) -> dict:
        return call(
            bridge,
            "geometry_nodes_reroute_move",
            {"group": group, "positions": [p.model_dump() for p in positions], "absolute": absolute},
        )

    @mcp.tool(
        name="blender_geometry_nodes_reroute_delete",
        description=(
            "Delete explicitly named NodeReroute nodes and reconnect what fed them to every downstream "
            "target, so no wire is lost and no socket changes. Reroute chains are handled transitively "
            "(deleting R1 and R2 of R1 -> R2 -> target reconnects R1's source to that target), branches "
            "keep all of their targets, and each affected multi-input socket keeps its exact link order. "
            "A reroute with no input link is refused (reroute_input_missing) instead of being removed "
            "silently, and a delete that would need the same source twice in one multi-input socket is "
            "refused (delete_would_merge_sources) rather than merging two independent sources. Every edit "
            "is re-verified against the resolved data flow and rolled back on failure."
        ),
    )
    def blender_geometry_nodes_reroute_delete(group: str, nodes: list[str]) -> dict:
        return call(bridge, "geometry_nodes_reroute_delete", {"group": group, "nodes": nodes})


__all__ = ["register"]
