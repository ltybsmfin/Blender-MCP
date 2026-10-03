"""Driver tools for the four supported owners: object, shape_keys, node_group, scene.

Semantics that matter when driving values from a tool:

* Drivers are data-block animation: they are evaluated when the dependency graph
  updates, i.e. on a frame change, on a property change that feeds them, or on an
  explicit ``blender_update_depsgraph``.  Reading a plain property back right
  after adding a driver does not by itself prove anything - the returned
  ``evaluated_value`` field is read after a forced depsgraph update.
* ``data_path`` strings are resolved with Blender's own ``ID.path_resolve``; they
  are property paths, not Python.  This server never evaluates Python on behalf
  of a driver.
* Only ``SINGLE_PROP`` (read one property of one ID) and ``TRANSFORMS`` (read a
  transform channel of one object) variables are supported, because those are
  the two that can be described and validated structurally.
* Existing, unrelated animation is preserved: adding or updating a driver only
  touches the F-Curve at the requested data path/index.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from ..bridge import BridgeClient
from ..models import DriverVariableSpec
from ._common import call

OwnerKind = Literal["object", "shape_keys", "node_group", "scene"]


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_inspect_drivers",
        description=(
            "List every driver on an owner: data path, array index, mute flag, driver type, expression, "
            "is_valid, keyframe count and, per variable, its name, type and targets (id_type, resolved "
            "id_name, data_path, transform type/space, bone). owner_kind='object' uses owner_name as the "
            "object; 'shape_keys' uses owner_name as the object whose Key datablock is driven; "
            "'node_group' uses owner_name as the node tree (e.g. an input socket default); 'scene' takes "
            "an optional scene name."
        ),
    )
    def blender_inspect_drivers(
        owner_kind: OwnerKind,
        owner_name: str | None = None,
    ) -> dict:
        return call(bridge, "inspect_drivers", {"owner_kind": owner_kind, "owner_name": owner_name})

    @mcp.tool(
        name="blender_add_driver",
        description=(
            "Add an expression driver. data_path is an RNA path on the owner, e.g. 'rotation_euler' with "
            "index=2, '[\"phase\"]' for a custom property, or 'key_blocks[\"Bulge\"].value' on "
            "owner_kind='shape_keys'; on owner_kind='node_group' use e.g. "
            "'nodes[\"XForm\"].inputs[\"Translation\"].default_value' with index=0. Every path is resolved "
            "with ID.path_resolve first. Variables are validated structurally: SINGLE_PROP targets need "
            "id_type/id_name and a resolvable data_path; TRANSFORMS targets need an OBJECT and a "
            "transform_type. The expression is checked against the declared variable names and a small "
            "function whitelist (no Python statements, no __dunder, no import/eval/exec), then the "
            "dependency graph is updated so the returned evaluated_value is real. An existing driver at "
            "the same path/index is an error unless replace=true"
        ),
    )
    def blender_add_driver(
        owner_kind: OwnerKind,
        data_path: str,
        expression: str,
        owner_name: str | None = None,
        index: int = -1,
        variables: list[DriverVariableSpec] | None = None,
        replace: bool = False,
    ) -> dict:
        return call(
            bridge,
            "add_driver",
            {
                "owner_kind": owner_kind,
                "owner_name": owner_name,
                "data_path": data_path,
                "index": index,
                "expression": expression,
                "variables": [spec.model_dump() for spec in (variables or [])],
                "replace": replace,
            },
        )

    @mcp.tool(
        name="blender_update_driver",
        description=(
            "Update an existing driver's expression and/or variables, or mute it. Variables already on the "
            "driver are replaced when you pass 'variables' (set replace_variables=true to be explicit "
            "about it). Other F-Curves and other drivers are untouched."
        ),
    )
    def blender_update_driver(
        owner_kind: OwnerKind,
        data_path: str,
        owner_name: str | None = None,
        index: int = -1,
        expression: str | None = None,
        variables: list[DriverVariableSpec] | None = None,
        replace_variables: bool = False,
        mute: bool | None = None,
    ) -> dict:
        return call(
            bridge,
            "update_driver",
            {
                "owner_kind": owner_kind,
                "owner_name": owner_name,
                "data_path": data_path,
                "index": index,
                "expression": expression,
                "variables": [spec.model_dump() for spec in variables] if variables is not None else None,
                "replace_variables": replace_variables,
                "mute": mute,
            },
        )

    @mcp.tool(
        name="blender_remove_driver",
        description=(
            "Remove the driver at one data path/index and report the remaining drivers on that owner. "
            "index=-1 removes every channel of the data path; pass an explicit index to remove a single "
            "channel. Removing a driver that does not exist is an error, not a silent no-op."
        ),
    )
    def blender_remove_driver(
        owner_kind: OwnerKind,
        data_path: str,
        owner_name: str | None = None,
        index: int = -1,
    ) -> dict:
        return call(
            bridge,
            "remove_driver",
            {"owner_kind": owner_kind, "owner_name": owner_name, "data_path": data_path, "index": index},
        )
