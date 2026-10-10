"""Typed native Outliner tools: collection-DAG queries and membership edits.

Every tool forwards to exactly one ``outliner_*`` bridge command; this module never
imports ``bpy`` and never touches the file system.  Validation of names, booleans
and lists is declared in the signatures (so the MCP schema is strict) and enforced
authoritatively inside Blender, which reports stable error codes.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ..bridge import BridgeClient
from ._common import call

#: Exact Blender ID names (collection/object/scene names).
Name = Annotated[str, Field(min_length=1, max_length=255)]
#: A collection path, ``"/"``-separated; ``""`` addresses an existing scene root.
Path = Annotated[str, Field(max_length=1024)]
Paths = Annotated[list[Path], Field(max_length=64)]
ObjectNames = Annotated[list[Name], Field(min_length=1, max_length=512)]
Bool = Annotated[bool, Field(strict=True)]
BoolOrNone = Annotated[bool | None, Field(strict=True)]

DOMAIN = "outliner"


def _meta(mutating: bool, idempotent: bool) -> dict:
    """The ``_meta`` block of every Outliner tool (domain marker + contract flags)."""
    return {
        "domain": DOMAIN,
        "bridge_prefix": "outliner_",
        "mutating": mutating,
        "idempotent": idempotent,
        "deletes_data": False,
    }


def register_outliner_tools(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_outliner_hierarchy",
        title="Outliner: collection hierarchy and object ownership",
        description=(
            "Read the collection DAG and exact view-layer names of one EXACT scene (never the active scene). From Scene.collection it "
            "returns every root-relative path ('' is the root), each collection's direct objects, its direct "
            "parent collections file-wide, every scene that links it, its child collections and visibility "
            "flags. A collection with several parents is reported as multi_parent (a DAG, not a cycle); real "
            "cycles are listed separately; every traversal and payload is bounded and reports truncated=true "
            "when a limit was hit. With include_memberships=true each object also reports its global ownership "
            "(all owning collections with their scenes/scene_paths, orphan flag). Geometry node groups are "
            "global datablocks, reported as such and never as collection members. Read-only."
        ),
        annotations=ToolAnnotations(
            title="Outliner: collection hierarchy and object ownership",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(False, True),
    )
    def blender_outliner_hierarchy(scene: Name, include_memberships: Bool = True) -> dict:
        return call(bridge, "outliner_hierarchy",
                    {"scene": scene, "include_memberships": include_memberships})

    @mcp.tool(
        name="blender_outliner_ensure_path",
        title="Outliner: ensure a collection path exists",
        description=(
            "Create the missing segments of a collection path below Scene.collection of one exact scene. "
            "Names are exact and '/' separated; at least one segment is required ('' is refused here because "
            "it only addresses an existing root elsewhere). Existing segments are reused idempotently. Blender's "
            "automatic '.001' rename is never accepted: a requested name that already exists elsewhere in the "
            "file fails with collection_name_conflict before anything is created. Every existing segment must "
            "belong to exactly this scene, have one direct parent and not be linked/overridden "
            "(collection_shared_across_scenes, collection_multi_parent, read_only_collection, "
            "collection_outside_scene). If a later segment fails, only the collections created by this same "
            "call are removed again (cleanup is reported, kept data is named). Inert: no object is linked."
        ),
        annotations=ToolAnnotations(
            title="Outliner: ensure a collection path exists",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_ensure_path(scene: Name, path: Path, create_missing: Bool = True) -> dict:
        return call(bridge, "outliner_ensure_path",
                    {"scene": scene, "path": path, "create_missing": create_missing})

    @mcp.tool(
        name="blender_outliner_link_objects",
        title="Outliner: link objects into a collection",
        description=(
            "Link exact object names into the collection at path of one exact scene ('' is the scene root). "
            "The whole batch is validated before the first change: unknown names fail with object_not_found, "
            "duplicated entries with duplicate_object and globally ambiguous names (a local and a linked "
            "datablock with the same name) with ambiguous_object. An object that is already in that collection "
            "is skipped idempotently. Additive only: no other membership is unlinked, and no object data, "
            "transform, modifier, Geometry Nodes group, material or link is touched; nothing is ever deleted. "
            "The target collection must belong to exactly this scene and have a single parent. Returns the "
            "ownership of every named object before and after."
        ),
        annotations=ToolAnnotations(
            title="Outliner: link objects into a collection",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_link_objects(scene: Name, path: Path, objects: ObjectNames) -> dict:
        return call(bridge, "outliner_link_objects",
                    {"scene": scene, "path": path, "objects": objects})

    @mcp.tool(
        name="blender_outliner_move_objects",
        title="Outliner: move objects between collections",
        description=(
            "Link exact objects into the collection at path and unlink them from exactly the same-scene "
            "collections named in unlink_from (required; an empty list means link only; '' is the scene root). "
            "Only the listed memberships are removed - every other collection that owns an object is left "
            "alone. The whole batch is validated and its result precomputed before the first change: a source "
            "path equal to path fails with conflicting_paths, a repeated source with duplicate_path, and a "
            "batch that would leave an object in no scene with orphan_object (nothing is changed). Objects "
            "already in the target are not re-linked. Object datablocks stay untouched - data, transforms, "
            "modifiers, Geometry Nodes groups, materials and links are never modified and nothing is deleted. "
            "Returns ownership before and after, plus the precomputed plan."
        ),
        annotations=ToolAnnotations(
            title="Outliner: move objects between collections",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_move_objects(scene: Name, path: Path, objects: ObjectNames,
                                      unlink_from: Paths) -> dict:
        return call(bridge, "outliner_move_objects",
                    {"scene": scene, "path": path, "objects": objects, "unlink_from": unlink_from})

    @mcp.tool(
        name="blender_outliner_unlink_objects",
        title="Outliner: unlink objects from a collection",
        description=(
            "Remove only the membership of exact object names in the collection at path of one exact scene "
            "('' is the scene root). Objects that are not linked there are skipped idempotently and no other "
            "membership is touched. The whole batch is precomputed first: if it would leave an object in no "
            "collection or in no scene, the call fails with orphan_object and changes nothing; with "
            "confirm_orphan=true the link is removed anyway, the object datablock is preserved and a warning "
            "is returned. Nothing is ever deleted, and no object data, transform, modifier, Geometry Nodes "
            "group, material or link is modified. Returns ownership before and after."
        ),
        annotations=ToolAnnotations(
            title="Outliner: unlink objects from a collection",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_unlink_objects(scene: Name, path: Path, objects: ObjectNames,
                                        confirm_orphan: Bool = False) -> dict:
        return call(bridge, "outliner_unlink_objects",
                    {"scene": scene, "path": path, "objects": objects, "confirm_orphan": confirm_orphan})

    @mcp.tool(
        name="blender_outliner_set_visibility",
        title="Outliner: set collection visibility flags",
        description=(
            "Set the native Collection.hide_viewport and/or Collection.hide_render of the collection at path "
            "of one exact scene ('' is the scene root). At least one flag must be provided; an omitted flag is "
            "left untouched, and a non-boolean value is refused with invalid_argument. The collection must "
            "belong to exactly this scene and have a single parent (collection_shared_across_scenes, "
            "collection_multi_parent, read_only_collection otherwise). Values are read back and verified; "
            "before/after are returned and a repeated call is idempotent. Only these two native flags are "
            "written: no object visibility, no collection membership and no data is changed or deleted."
        ),
        annotations=ToolAnnotations(
            title="Outliner: set collection visibility flags",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_set_visibility(scene: Name, path: Path, hide_viewport: BoolOrNone = None,
                                        hide_render: BoolOrNone = None) -> dict:
        return call(bridge, "outliner_set_visibility",
                    {"scene": scene, "path": path,
                     "hide_viewport": hide_viewport, "hide_render": hide_render})


#: Alias so the module also works with the ``module.register(mcp, bridge)`` convention.
register = register_outliner_tools
