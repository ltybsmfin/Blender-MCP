"""Optional plugin tools: only the Blender-side adapter probes/imports bpy."""
from __future__ import annotations
from ._common import call
from .node_frames import Name, Body


def register(mcp, bridge):
    @mcp.tool(name="blender_node_note_status", description="Probe optional Mod node-notes extension inside Blender: version, source path, enabled state and RNA operators. Native Frame tools remain available when missing.")
    def status() -> dict:
        return call(bridge, "node_note_status")

    @mcp.tool(name="blender_node_note_extension", description="Enable/disable the fixed local node-notes extension repository under Mod, without copying source. persist=false by default; persistent preferences require a project-owned profile.")
    def extension(enable: bool = True, persist: bool = False) -> dict:
        return call(bridge, "node_note_extension", dict(enable=enable, persist=persist))

    @mcp.tool(name="blender_node_note_create", description="Create a managed native Frame annotation with its own internal Text in exact group. Reject name collisions; no ordinary Frames adopted.")
    def create(group: Name, frame: Name, title: str = "", body: Body = "", location: tuple[float, float] = (0, 0)) -> dict:
        return call(bridge, "node_note_create", dict(group=group, frame=frame, title=title, body=body, location=location))

    @mcp.tool(name="blender_node_note_get", description="Read one plugin-managed annotation Frame/Text. Plugin absent/disabled gives a structured dependency error.")
    def get(group: Name, frame: Name) -> dict:
        return call(bridge, "node_note_get", dict(group=group, frame=frame))

    @mcp.tool(name="blender_node_note_update", description="Noninteractive body/title update of a managed annotation. Keeps Text identity, refuses shared Text. Empty requires allow_empty=true.")
    def update(group: Name, frame: Name, body: Body, title: str | None = None, allow_empty: bool = False) -> dict:
        return call(bridge, "node_note_update", dict(group=group, frame=frame, body=body, title=title, allow_empty=allow_empty))

    @mcp.tool(name="blender_node_note_delete", description="Delete only a plugin-managed annotation with confirm=true. Ordinary Frames protected. Text retained unless remove_text=true and sole ownership verified. Child canvas positions retained.")
    def delete(group: Name, frame: Name, confirm: bool = False, remove_text: bool = False) -> dict:
        return call(bridge, "node_note_delete", dict(group=group, frame=frame, confirm=confirm, remove_text=remove_text))
