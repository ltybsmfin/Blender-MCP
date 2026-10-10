"""Typed native Frame tools; independent of optional extensions."""
from __future__ import annotations
from typing import Annotated
from pydantic import Field
from ._common import call

Name = Annotated[str, Field(min_length=1, max_length=255)]
Names = Annotated[list[Name], Field(min_length=1, max_length=512)]
Body = Annotated[str, Field(max_length=16384)]


def register(mcp, bridge):
    @mcp.tool(name="blender_node_frame_create", description="Create a native Frame in an exact node-group datablock. Existing Frame is reused unchanged. No annotation extension required.")
    def create(group: Name, frame: Name, label: str = "", location: tuple[float, float] = (0, 0), color: tuple[float, float, float] | None = None, label_size: int = 24, shrink: bool = False) -> dict:
        return call(bridge, "node_frame_create", dict(group=group, frame=frame, label=label, location=location, color=color, label_size=label_size, shrink=shrink))

    @mcp.tool(name="blender_node_frame_get", description="Read native Frame label, layout, children, Text name/body/references. Exact group and frame names.")
    def get(group: Name, frame: Name) -> dict:
        return call(bridge, "node_frame_get", dict(group=group, frame=frame))

    @mcp.tool(name="blender_node_frame_parent", description="Atomically parent exact node names to a native Frame; preserve canvas positions, ports and links. Reject cycles. create_frame defaults false.")
    def parent(group: Name, frame: Name, nodes: Names, create_frame: bool = False) -> dict:
        return call(bridge, "node_frame_parent", dict(group=group, frame=frame, nodes=nodes, create_frame=create_frame))

    @mcp.tool(name="blender_node_frame_unparent", description="Detach only requested direct children of this Frame; preserve canvas positions and all calculation links. Other parents skipped.")
    def unparent(group: Name, frame: Name, nodes: Names) -> dict:
        return call(bridge, "node_frame_unparent", dict(group=group, frame=frame, nodes=nodes))

    @mcp.tool(name="blender_node_frame_update", description="Set native Frame label/color/label_size(8..64)/shrink/width/height. Omitted properties preserved. No node graph rearrangement.")
    def update(group: Name, frame: Name, label: str | None = None, color: tuple[float, float, float] | None = None, label_size: int | None = None, shrink: bool | None = None, width: float | None = None, height: float | None = None) -> dict:
        return call(bridge, "node_frame_update", dict(group=group, frame=frame, label=label, color=color, label_size=label_size, shrink=shrink, width=width, height=height))

    @mcp.tool(name="blender_node_frame_set_text", description="Create/update stable internal Text for native Frame. User text requires overwrite=true; shared/external text refused. Empty body requires allow_empty=true and retains Text. Does not delete data.")
    def set_text(group: Name, frame: Name, body: Body, overwrite: bool = False, allow_empty: bool = False) -> dict:
        return call(bridge, "node_frame_set_text", dict(group=group, frame=frame, body=body, overwrite=overwrite, allow_empty=allow_empty))

    @mcp.tool(name="blender_node_frame_layout", description="Read nodes/Frames/parent relationships/canvas positions/labels/text/links of one exact group. Never auto-layouts.")
    def layout(group: Name) -> dict:
        return call(bridge, "node_frame_layout", dict(group=group))
