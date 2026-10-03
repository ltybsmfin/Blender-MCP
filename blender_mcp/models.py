"""Typed argument models shared by the MCP tools.

The models exist so the MCP client (Codex) sees a precise JSON schema instead of
opaque objects, and so obviously wrong shapes fail before a Blender round trip.
Values that are genuinely polymorphic (socket defaults, driver expressions'
operands) stay ``Any`` and are validated with explicit messages on the Blender
side, which has the authoritative RNA knowledge.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Interface socket types accepted by ``node_tree.interface.new_socket``.
SocketTypeName = Literal[
    "NodeSocketFloat",
    "NodeSocketInt",
    "NodeSocketBool",
    "NodeSocketVector",
    "NodeSocketColor",
    "NodeSocketRotation",
    "NodeSocketString",
    "NodeSocketMenu",
    "NodeSocketGeometry",
    "NodeSocketMaterial",
    "NodeSocketObject",
    "NodeSocketCollection",
    "NodeSocketImage",
]


class InterfaceSocketSpec(BaseModel):
    """One Geometry Nodes group interface socket."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Socket label shown in the modifier / group UI.")
    socket_type: SocketTypeName = Field(description="Blender interface socket type.")
    in_out: Literal["INPUT", "OUTPUT"] = "INPUT"
    default_value: Any = Field(
        default=None,
        description="Socket default: number, bool, string or vector list, e.g. [1.0, 0.0, 0.0].",
    )
    min_value: float | None = Field(default=None, description="UI soft minimum (numeric sockets).")
    max_value: float | None = Field(default=None, description="UI soft maximum (numeric sockets).")
    description: str | None = None
    subtype: str | None = Field(
        default=None,
        description="Blender subtype, e.g. 'DISTANCE', 'ANGLE', 'FACTOR', 'PERCENTAGE', 'TRANSLATION'.",
    )
    hide_value: bool | None = Field(default=None, description="Hide the inline value widget.")
    hide_in_modifier: bool | None = Field(default=None, description="Do not expose on the modifier.")
    default_attribute_name: str | None = None


class DriverTargetSpec(BaseModel):
    """One driver variable target (``DriverTarget`` RNA)."""

    model_config = ConfigDict(extra="forbid")

    id_type: str = Field(
        default="OBJECT",
        description="Target ID type: OBJECT, SCENE, KEY, MESH, NODETREE, MATERIAL, COLLECTION, ...",
    )
    id_name: str | None = Field(
        default=None,
        description="Datablock name inside that ID type (e.g. the object name). Required for all but SCENE.",
    )
    data_path: str | None = Field(
        default=None,
        description=(
            "SINGLE_PROP only: an RNA path on the target ID, validated with ID.path_resolve, "
            "e.g. '[\"phase\"]' for a custom property or 'location'."
        ),
    )
    transform_type: str | None = Field(
        default=None,
        description="TRANSFORMS only: LOC_X, LOC_Y, LOC_Z, ROT_X, ROT_Y, ROT_Z, SCALE_X, ...",
    )
    transform_space: str | None = Field(
        default="WORLD_SPACE", description="TRANSFORMS only: WORLD_SPACE or LOCAL_SPACE."
    )
    bone_target: str | None = Field(default=None, description="TRANSFORMS only: optional bone name.")


class DriverVariableSpec(BaseModel):
    """One driver variable, with its targets."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Variable name used inside the driver expression.")
    type: Literal["SINGLE_PROP", "TRANSFORMS"] = "SINGLE_PROP"
    targets: list[DriverTargetSpec] = Field(
        default_factory=list, description="At least one target; TRANSFORMS uses exactly one."
    )


class GraphOperation(BaseModel):
    """One ordered geometry-nodes edit for ``blender_batch_graph_edit``.

    ``op`` selects the operation; the remaining fields are the same arguments as
    the corresponding single-purpose tool.  Unknown extra keys are passed
    through to Blender so the batch cannot silently drop an argument.
    """

    model_config = ConfigDict(extra="allow")

    op: Literal[
        "create_node",
        "set_node_property",
        "set_socket_default",
        "link_sockets",
        "unlink_socket",
        "remove_node",
    ] = Field(description="Operation to perform, applied in list order.")

    node_type: str | None = Field(default=None, description="create_node: e.g. 'GeometryNodeTransform'.")
    name: str | None = Field(default=None, description="create_node: explicit node name.")
    label: str | None = None
    location: list[float] | None = Field(default=None, description="create_node: [x, y] in the node editor.")
    settings: dict[str, Any] | None = Field(
        default=None, description="create_node/set_node_property: RNA property values."
    )
    node: str | int | None = Field(default=None, description="Node name (str) or node index (int).")
    property: str | None = Field(default=None, description="set_node_property: RNA property identifier.")
    value: Any = Field(default=None, description="Value to assign (socket default or node property).")
    socket: str | int | None = Field(
        default=None, description="Socket identifier, display name or index (int)."
    )
    in_out: str | None = Field(default=None, description="'INPUT' (default) or 'OUTPUT'.")
    from_node: str | int | None = None
    from_socket: str | int | None = None
    to_node: str | int | None = None
    to_socket: str | int | None = None
    replace: bool | None = Field(
        default=None, description="link_sockets: relink an already linked input socket."
    )

    def to_bridge_params(self) -> dict:
        """Drop ``op`` and every unset field."""
        data = self.model_dump(exclude_none=True)
        data.pop("op", None)
        return data


class DriverSpec(BaseModel):
    """Driver description returned by the inspect/add/update tools."""

    model_config = ConfigDict(extra="allow")
