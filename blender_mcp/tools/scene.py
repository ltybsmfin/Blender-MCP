"""Scene / object introspection, mesh creation, custom properties and saving."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import config
from ..bridge import BridgeClient
from ._common import call


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_present_object",
        description="Select and frame an object in the GUI 3D viewport, optionally isolate it in local view and request foreground activation of THIS Blender window. Unlike capture_preview this intentionally keeps the new view and selection; no geometry is changed. No desktop input automation.",
    )
    def blender_present_object(
        object: str,
        view: Literal["current", "isometric", "front", "back", "right", "top"] = "isometric",
        isolate: bool = True,
        foreground: bool = False,
    ) -> dict:
        return call(bridge, "present_object", {"object": object, "view": view, "isolate": isolate, "foreground": foreground})

    @mcp.tool(
        name="blender_scene_summary",
        description=(
            "Overview of the loaded scene: frame range, collections, materials, node groups (with node "
            "counts and user counts) and per-object rows (type, vertex/polygon counts, modifier types, "
            "shape key count, driver count, custom property keys). Start here to discover exact names."
        ),
    )
    def blender_scene_summary(include_objects: bool = True) -> dict:
        return call(bridge, "scene_summary", {"include_objects": include_objects})

    @mcp.tool(
        name="blender_list_objects",
        description=(
            "List objects, optionally filtered by substring of the name and/or exact Blender type "
            "(MESH, CURVE, EMPTY, ...). Returns a compact row per object."
        ),
    )
    def blender_list_objects(
        name_contains: str | None = None,
        type: str | None = None,
        limit: int = 200,
    ) -> dict:
        return call(
            bridge,
            "list_objects",
            {"name_contains": name_contains, "type": type, "limit": limit},
        )

    @mcp.tool(
        name="blender_object_info",
        description=(
            "Deep introspection of one object: transforms, world matrix, custom properties, modifiers "
            "with their geometry-nodes parameters, vertex groups, materials, shape key inventory with "
            "topology consistency, drivers, and - importantly - the EVALUATED mesh (vertex/edge/polygon "
            "counts, world-space bounds and a small signature) so you can verify what modifiers actually "
            "produce instead of guessing."
        ),
    )
    def blender_object_info(name: str, include_evaluated: bool = True) -> dict:
        return call(bridge, "object_info", {"name": name, "include_evaluated": include_evaluated})

    @mcp.tool(
        name="blender_create_mesh_object",
        description=(
            "Create a mesh object from explicit vertex/face data (no operators, no interactive context). "
            "Faces must reference valid vertex indices; n-gons are allowed. Use this when you need "
            "exactly controlled topology - for example a mesh that will receive shape keys."
        ),
    )
    def blender_create_mesh_object(
        name: str,
        vertices: list[list[float]],
        faces: list[list[int]] | None = None,
        edges: list[list[int]] | None = None,
        collection: str | None = None,
        location: list[float] | None = None,
    ) -> dict:
        return call(
            bridge,
            "create_mesh_object",
            {
                "name": name,
                "vertices": vertices,
                "faces": faces,
                "edges": edges,
                "collection": collection,
                "location": location,
            },
        )

    @mcp.tool(
        name="blender_create_primitive",
        description=(
            "Create a primitive mesh object through Blender's own operators: cube, plane, uv_sphere, "
            "ico_sphere, cylinder, cone, torus, grid, monkey. Note that operator primitives give you "
            "their own topology; if you later need shape keys, the key data must match that topology."
        ),
    )
    def blender_create_primitive(
        kind: Literal[
            "cube", "plane", "uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "grid", "monkey"
        ],
        name: str | None = None,
        location: list[float] | None = None,
        rotation: list[float] | None = None,
        scale: list[float] | None = None,
        size: float | None = None,
        radius: float | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict:
        return call(
            bridge,
            "create_primitive",
            {
                "kind": kind,
                "name": name,
                "location": location,
                "rotation": rotation,
                "scale": scale,
                "size": size,
                "radius": radius,
                "parameters": parameters,
            },
        )

    @mcp.tool(
        name="blender_set_custom_property",
        description=(
            "Set, read back or remove an ID custom property on an object or the scene. Custom properties "
            "are the intended driver inputs: create one (e.g. phase = 0.0), then point a driver variable "
            "at the path '[\"phase\"]'. Lists are stored as float arrays (handy for vectors)."
        ),
    )
    def blender_set_custom_property(
        owner_kind: Literal["object", "scene"] = "object",
        key: str = "",
        value: Any = None,
        owner_name: str | None = None,
        remove: bool = False,
    ) -> dict:
        return call(
            bridge,
            "set_custom_property",
            {
                "owner_kind": owner_kind,
                "key": key,
                "value": value,
                "owner_name": owner_name,
                "remove": remove,
            },
        )

    @mcp.tool(
        name="blender_save_blend",
        description=(
            "Save the current file as <workspace>/Project/<relative_path>. The path is resolved and must "
            "stay inside the project root: absolute paths, '..' escapes and symlink/junction components "
            "are rejected. overwrite defaults to false and an existing file is an error until you pass "
            "overwrite=true explicitly."
        ),
    )
    def blender_save_blend(
        relative_path: str,
        overwrite: bool = False,
        compress: bool = False,
    ) -> dict:
        # Fail early on an unsafe path so a bad request never reaches Blender.
        try:
            resolved = config.resolve_project_path(relative_path)
        except config.PathValidationError as exc:
            raise ToolError("invalid_path: %s (path=%r)" % (exc, relative_path)) from exc
        if not overwrite and resolved.exists():
            raise ToolError(
                "file_exists: %s already exists and overwrite=false; pass overwrite=true to replace it "
                "(nothing was written)" % resolved
            )
        return call(
            bridge,
            "save_blend",
            {"relative_path": relative_path, "overwrite": overwrite, "compress": compress},
        )

    @mcp.tool(
        name="blender_set_frame",
        description=(
            "Move the scene to a frame (this re-evaluates drivers and the dependency graph). Drivers only "
            "re-evaluate on frame changes or dependency-graph updates, so call this after editing driver "
            "variables or when a value must be sampled at a specific frame."
        ),
    )
    def blender_set_frame(frame: int, subframe: float = 0.0) -> dict:
        return call(bridge, "set_frame", {"frame": frame, "subframe": subframe})

    @mcp.tool(
        name="blender_update_depsgraph",
        description=(
            "Force a dependency-graph update and report the current frame. Use it after direct datablock "
            "edits (custom properties, key coordinates) when you want evaluated results to reflect them."
        ),
    )
    def blender_update_depsgraph() -> dict:
        return call(bridge, "update_depsgraph")

    @mcp.tool(
        name="blender_environment",
        description=(
            "Paths and versions of this MCP server without touching Blender: venv interpreter, workspace "
            "root, project root, token file presence, bridge address, timeout, and the bootstrap script "
            "that must run inside Blender."
        ),
    )
    def blender_environment() -> dict:
        payload = config.describe_environment()
        try:
            payload["blender_executable"] = str(config.resolve_blender_executable())
        except FileNotFoundError as exc:
            payload["blender_executable"] = None
            payload["blender_executable_error"] = str(exc)
        return payload
