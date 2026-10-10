"""Typed native Outliner *management* tools: Collection lifecycle, ordinary Mesh/Empty
object create/rename/delete and the Outliner display switches.

Each of the nine tools forwards to exactly one ``outliner_*`` bridge command; the
bridge command name is the tool name without the ``blender_`` prefix.  This module
never imports ``bpy`` and never touches the file system.  Types, names, booleans and
geometry are declared in the signatures (so the MCP schema is strict) and enforced
authoritatively inside Blender, which reports stable error codes.

The objects created here are **ordinary MESH/EMPTY objects**; Collection Instances
are not part of this package, and object inspection stays with the existing core
``blender_object_info`` tool.  Object *movement* between collections uses the existing
``blender_outliner_move_objects`` command (it accepts ordinary objects), and 3D
transforms are deliberately out of scope for the Outliner package.

The commands themselves live in ``blender/outliner_management.py`` and reuse the
collection primitives published by ``blender/outliner.py``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ..bridge import BridgeClient
from ._common import call

#: Exact Blender ID names (collection/object/scene/view-layer names).
Name = Annotated[str, Field(min_length=1, max_length=255)]
#: A collection path, ``"/"``-separated; ``""`` addresses an existing scene root.
Path = Annotated[str, Field(max_length=1024)]
ObjectNames = Annotated[list[Name], Field(min_length=1, max_length=512)]
Bool = Annotated[bool, Field(strict=True)]
BoolOrNone = Annotated[bool | None, Field(strict=True)]
#: Object types this package allocates; Collection Instances are not created here.
ObjectType = Literal["MESH", "EMPTY"]
#: One finite three-component vertex; the bridge refuses NaN/infinity.
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Vertex = Annotated[list[FiniteNumber], Field(min_length=3, max_length=3)]
Vertices = Annotated[list[Vertex], Field(max_length=20000)]
#: One polygon: at least a triangle, integer indices validated against the vertices.
FaceIndex = Annotated[int, Field(strict=True)]
Face = Annotated[list[FaceIndex], Field(min_length=3, max_length=10000)]
Faces = Annotated[list[Face], Field(max_length=20000)]

DOMAIN = "outliner"

#: Tool name -> bridge command (the bridge drops the ``blender_`` prefix).
TOOL_COMMANDS = {
    "blender_outliner_collection_create": "outliner_collection_create",
    "blender_outliner_collection_rename": "outliner_collection_rename",
    "blender_outliner_collection_move": "outliner_collection_move",
    "blender_outliner_collection_delete": "outliner_collection_delete",
    "blender_outliner_object_create": "outliner_object_create",
    "blender_outliner_object_rename": "outliner_object_rename",
    "blender_outliner_object_delete": "outliner_object_delete",
    "blender_outliner_collection_display": "outliner_collection_display",
    "blender_outliner_object_display": "outliner_object_display",
}


def _meta(mutating: bool, idempotent: bool, deletes_data: bool = False, **extra) -> dict:
    """The ``_meta`` block of every management tool (domain marker + contract flags)."""
    meta = {
        "domain": DOMAIN,
        "bridge_prefix": "outliner_",
        "mutating": mutating,
        "idempotent": idempotent,
        "deletes_data": deletes_data,
        "collection_primitives": "blender/outliner.py:register_commands(api)",
        "object_kind": "ordinary_mesh_empty",
    }
    meta.update(extra)
    return meta


def _display_meta(per_view_layer_flags: list) -> dict:
    """Meta for the two display tools (all switches are written, monitor included)."""
    return _meta(
        True,
        True,
        deletes_data=False,
        per_view_layer=True,
        monitor_property_written=True,
        per_view_layer_flags=list(per_view_layer_flags),
        datablock_flags=["disable_viewport", "hide_render"],
    )


def register_outliner_management_tools(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_outliner_collection_create",
        title="Outliner: create a collection",
        description=(
            "Create one exactly named collection below the collection at parent_path of one exact scene "
            "('' is the scene root). The parent is resolved first and must belong to exactly this scene "
            "with a single parent and no library link. The requested name is checked file-wide BEFORE "
            "anything is created: an existing collection with that exact name somewhere else fails with "
            "collection_name_conflict, because Blender would silently store a second one as 'name.001' - "
            "which this server never does (auto_suffix=false). A collection that already exists at exactly "
            "that parent/name is reused (created=[], reused=[name], idempotent=true) and nothing is "
            "allocated. The stored name is read back; a rename/truncation by Blender fails with invalid_name "
            "and removes the datablock created by this call. Only one collection datablock is created: no "
            "object, no membership, no flag and no Geometry Nodes group is touched."
        ),
        annotations=ToolAnnotations(
            title="Outliner: create a collection",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_collection_create(scene: Name, parent_path: Path, name: Name) -> dict:
        return call(bridge, "outliner_collection_create",
                    {"scene": scene, "parent_path": parent_path, "name": name})

    @mcp.tool(
        name="blender_outliner_collection_rename",
        title="Outliner: rename a collection",
        description=(
            "Rename the collection at path of one exact scene to an exact name. The scene root cannot be "
            "renamed (root_immutable) and the collection must belong to exactly this scene with a single "
            "parent and no library link. The requested name is checked file-wide before the write, so a "
            "name that already exists elsewhere fails with collection_name_conflict instead of silently "
            "becoming 'name.001' (auto_suffix=false). The name is read back afterwards; if Blender stored "
            "something else, the previous name is restored where possible and the call fails with "
            "invalid_name. Renaming changes the address of the collection and of its descendants - their "
            "objects, memberships, data-blocks, Geometry Nodes groups and reference links are untouched. "
            "Repeating the call with the same name is idempotent."
        ),
        annotations=ToolAnnotations(
            title="Outliner: rename a collection",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_collection_rename(scene: Name, path: Path, name: Name) -> dict:
        return call(bridge, "outliner_collection_rename",
                    {"scene": scene, "path": path, "name": name})

    @mcp.tool(
        name="blender_outliner_collection_move",
        title="Outliner: move a collection",
        description=(
            "Move the collection at path under the collection at parent_path of one exact scene ('' is the "
            "scene root). Both collections are resolved and guarded first: source and target parent must "
            "belong to exactly this scene, have a single parent and no library link "
            "(read_only_collection, collection_shared_across_scenes, collection_multi_parent, "
            "collection_outside_scene). The scene root cannot be moved and a target inside the moved "
            "subtree - including the collection itself - is refused with cycle_hierarchy. The move links "
            "the target parent first and only then unlinks the previous parent, through the shared "
            "compensating batch primitive, so a failure restores the old relation and reports "
            "applied/compensated/residual. Objects, object data, transforms, modifiers, Geometry Nodes "
            "groups, materials and reference links are never touched; a move that is already done is "
            "idempotent (changed=false)."
        ),
        annotations=ToolAnnotations(
            title="Outliner: move a collection",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_collection_move(scene: Name, path: Path, parent_path: Path) -> dict:
        return call(bridge, "outliner_collection_move",
                    {"scene": scene, "path": path, "parent_path": parent_path})

    @mcp.tool(
        name="blender_outliner_collection_delete",
        title="Outliner: delete a collection",
        description=(
            "PERMANENTLY delete the collection at path of one exact scene. confirm_delete must be true "
            "(confirmation_required otherwise) and the scene root is refused (root_immutable). By default "
            "a collection that still has children is refused with collection_has_children; with "
            "recursive=true exactly that subtree is deleted. The whole subtree is prevalidated before the "
            "first removal: library links (read_only_collection), extra scenes or parents "
            "(collection_shared_across_scenes, collection_multi_parent, collection_outside_scene), an "
            "external parent route, a fake user, a Collection Instance source, a Geometry Nodes reference, "
            "or any user beyond its direct parents (collection_in_use) all refuse the call with nothing "
            "changed. No object is ever deleted: objects that would leave the scene are linked to the scene "
            "root first (compensated on failure) and every other membership is preserved. Removal is "
            "permanent in Blender, so this command is NOT transactional - a failure after the first real "
            "removal is reported as outliner_delete_partial with deleted/residual/partial/"
            "not_transactional=true instead of pretending a rollback. Returns the deleted collections, the "
            "preserved objects with their original membership, the relinked objects and warnings."
        ),
        annotations=ToolAnnotations(
            title="Outliner: delete a collection",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        meta=_meta(True, False, deletes_data=True),
    )
    def blender_outliner_collection_delete(scene: Name, path: Path, confirm_delete: Bool,
                                           recursive: Bool = False) -> dict:
        return call(bridge, "outliner_collection_delete",
                    {"scene": scene, "path": path, "confirm_delete": confirm_delete,
                     "recursive": recursive})

    @mcp.tool(
        name="blender_outliner_object_create",
        title="Outliner: create an ordinary Mesh/Empty object",
        description=(
            "Create ONE ordinary object in the collection at path of one exact scene ('' is the scene "
            "root). type is 'MESH' (default) or 'EMPTY': a Mesh gets a real bpy.data.meshes data block, an "
            "Empty gets none - Collection Instances are not created by this package. For a Mesh, vertices "
            "([[x,y,z], ...]) and faces ([[i,j,k,...], ...]) are optional: omitting both creates an empty "
            "mesh data block that is ready for a Geometry Nodes modifier. Every value is validated before "
            "the first allocation: vertices must be finite three-number triples, every face needs at least "
            "three integer indices that index the supplied vertices, and the totals are bounded "
            "(invalid_vertices / invalid_faces). 'EMPTY' together with vertices/faces is refused "
            "(invalid_argument). Names are exact and never auto-suffixed: an existing object with that name "
            "fails with object_name_conflict and existing mesh data with that name fails with "
            "data_name_conflict (object and mesh names live in different ID namespaces, so an object and a "
            "mesh may share a name while two meshes may not). The mesh data name defaults to the object "
            "name, and both are read back: a rename by Blender fails with invalid_name and removes exactly "
            "the datablocks this call created (cleanup.removed/residual). Returns the object type, data "
            "name, membership and counts; no existing object, data block or Geometry Nodes group is "
            "modified."
        ),
        annotations=ToolAnnotations(
            title="Outliner: create an ordinary Mesh/Empty object",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        meta=_meta(True, False),
    )
    def blender_outliner_object_create(scene: Name, path: Path, name: Name,
                                       type: ObjectType = "MESH",
                                       vertices: Vertices | None = None,
                                       faces: Faces | None = None) -> dict:
        return call(bridge, "outliner_object_create",
                    {"scene": scene, "path": path, "name": name, "type": type,
                     "vertices": vertices, "faces": faces})

    @mcp.tool(
        name="blender_outliner_object_rename",
        title="Outliner: rename an ordinary object",
        description=(
            "Rename ONE ordinary object of one exact scene to an exact name. The object must be linked in "
            "exactly this scene and not linked/overridden (object_not_in_scene, "
            "object_shared_across_scenes, read_only_object); unknown or ambiguous names fail with "
            "object_not_found/ambiguous_object. The new name is checked file-wide before the write, so a "
            "collision fails with object_name_conflict instead of becoming 'name.001' "
            "(auto_suffix=false); the stored name is read back and the previous name is restored where "
            "possible if Blender stored something else (invalid_name). Only the Object name changes: the "
            "mesh/data block, modifiers, Geometry Nodes groups, drivers, shape keys, vertex groups, "
            "constraints, parent and collection membership are read before and after and reported in "
            "references (a difference would restore the old name and fail). Repeating the call with the "
            "same name is idempotent."
        ),
        annotations=ToolAnnotations(
            title="Outliner: rename an ordinary object",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_meta(True, True),
    )
    def blender_outliner_object_rename(scene: Name, object: Name, name: Name) -> dict:
        return call(bridge, "outliner_object_rename",
                    {"scene": scene, "object": object, "name": name})

    @mcp.tool(
        name="blender_outliner_object_delete",
        title="Outliner: delete ordinary objects",
        description=(
            "PERMANENTLY delete the exact Object datablocks named in objects of one exact scene. "
            "confirm_delete must be true (confirmation_required otherwise); the explicit object list plus "
            "that confirmation is the authorisation, there is no hidden permission parameter. The whole "
            "batch is validated BEFORE the first removal and a rejected batch deletes nothing: every "
            "object must be linked in exactly this scene (object_not_in_scene, "
            "object_shared_across_scenes), not linked/overridden (read_only_object) and not referenced "
            "anywhere else (object_in_use) - child objects (object.parent), constraint targets, modifier "
            "objects, Geometry Nodes object sockets and any other user reported by bpy.data.user_map or by "
            "Object.users beyond the object's direct collection ownership all refuse the call instead of "
            "deleting a referenced object. Only the Object data blocks are removed: mesh, material and "
            "Geometry Nodes data are preserved with zero users and orphan data is never purged. Removal is "
            "permanent in Blender, so this command is NOT transactional - a failure after the first real "
            "removal is reported as outliner_delete_partial with deleted/residual/partial/"
            "not_transactional=true and never reads a freed datablock."
        ),
        annotations=ToolAnnotations(
            title="Outliner: delete ordinary objects",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        meta=_meta(True, False, deletes_data=True),
    )
    def blender_outliner_object_delete(scene: Name, objects: ObjectNames,
                                       confirm_delete: Bool) -> dict:
        return call(bridge, "outliner_object_delete",
                    {"scene": scene, "objects": objects, "confirm_delete": confirm_delete})

    @mcp.tool(
        name="blender_outliner_collection_display",
        title="Outliner: set collection display switches in a view layer",
        description=(
            "Set the Outliner display switches of the collection at path of one exact scene, addressed IN "
            "one explicit view layer (the exact name is looked up in scene.view_layers - the active view "
            "layer and every other UI context are never used). Four switches, each set to true (switch the "
            "state on) or false (switch it back off); an omitted switch keeps its current value and an "
            "all-null call is refused with invalid_argument: hidden = the per-view-layer eye "
            "(LayerCollection.hide_viewport), exclude = the per-view-layer checkbox "
            "(LayerCollection.exclude), disable_viewport = the global monitor icon "
            "(Collection.hide_viewport) and hide_render = render visibility (Collection.hide_render). The "
            "two datablock-wide switches go through the single-owner guard, so a collection shared across "
            "scenes or with several parents is refused (collection_shared_across_scenes, "
            "collection_multi_parent, read_only_collection) while the per-view-layer switches only need the "
            "collection to be addressable in that view layer (collection_not_in_view_layer). Every switch "
            "is written through the compensating batch primitive, read back individually and returned as "
            "before/after; a value Blender did not store restores the previous state and fails with "
            "outliner_write_failed. Repeating the same values is idempotent."
        ),
        annotations=ToolAnnotations(
            title="Outliner: set collection display switches in a view layer",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_display_meta(["hidden", "exclude"]),
    )
    def blender_outliner_collection_display(scene: Name, path: Path, view_layer: Name,
                                            hidden: BoolOrNone = None,
                                            disable_viewport: BoolOrNone = None,
                                            hide_render: BoolOrNone = None,
                                            exclude: BoolOrNone = None) -> dict:
        return call(bridge, "outliner_collection_display",
                    {"scene": scene, "path": path, "view_layer": view_layer, "hidden": hidden,
                     "disable_viewport": disable_viewport, "hide_render": hide_render, "exclude": exclude})

    @mcp.tool(
        name="blender_outliner_object_display",
        title="Outliner: set object display switches in a view layer",
        description=(
            "Set the Outliner display switches of exact object names of one exact scene, addressed IN one "
            "explicit view layer (the exact name is looked up in scene.view_layers - never the active view "
            "layer, and objects shared across scenes are refused). Three switches, each true (on) or false "
            "(back off); an omitted switch keeps its value and an all-null call is refused with "
            "invalid_argument: hidden = the per-view-layer eye "
            "(Object.hide_set(hide, view_layer=...) / Object.hide_get(view_layer=...)), disable_viewport = "
            "the global monitor icon (Object.hide_viewport) and hide_render = render visibility "
            "(Object.hide_render, which is also how a camera is excluded from renders). The whole batch is "
            "validated first - every object must be linked in exactly this scene, singly scened and "
            "writable (object_not_in_scene, object_shared_across_scenes, read_only_object), unknown or "
            "ambiguous names fail with object_not_found/ambiguous_object - so a rejected batch writes "
            "nothing. Every switch is written through the compensating batch primitive, read back "
            "individually and returned as before/after; a value Blender did not store restores the previous "
            "state and fails with outliner_write_failed. No object data, modifier or Geometry Nodes group "
            "is modified, and repeating the same values is idempotent."
        ),
        annotations=ToolAnnotations(
            title="Outliner: set object display switches in a view layer",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=_display_meta(["hidden"]),
    )
    def blender_outliner_object_display(scene: Name, objects: ObjectNames, view_layer: Name,
                                        hidden: BoolOrNone = None,
                                        disable_viewport: BoolOrNone = None,
                                        hide_render: BoolOrNone = None) -> dict:
        return call(bridge, "outliner_object_display",
                    {"scene": scene, "objects": objects, "view_layer": view_layer, "hidden": hidden,
                     "disable_viewport": disable_viewport, "hide_render": hide_render})


#: Alias so the module also works with the ``module.register(mcp, bridge)`` convention.
register = register_outliner_management_tools
