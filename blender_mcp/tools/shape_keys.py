"""Shape key tools.

Topology rule, stated once and enforced everywhere: shape keys deform the
**original mesh vertices** of the object, in their stored order.  A key block
holds exactly one coordinate per mesh vertex, so:

* the object must be a MESH with at least one vertex;
* every key block must have the same vertex count as the mesh;
* 'Basis' is the reference: it is created automatically, defines the base
  position of every vertex and is protected from coordinate overwrite unless you
  explicitly ask for it;
* geometry produced by a modifier (in particular a Geometry Nodes graph that
  generates its own mesh) is NOT affected by shape keys, because those output
  vertices are not the original vertices.  If you need the two effects to
  combine, keep them on separate objects - one object with shape keys, one
  object with the procedural modifier - instead of stacking them.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..bridge import BridgeClient
from ._common import call


def register(mcp: FastMCP, bridge: BridgeClient) -> None:
    @mcp.tool(
        name="blender_inspect_shape_keys",
        description=(
            "Shape key inventory of a mesh object: Basis and every key with index, influence value, mute "
            "state, slider range, relative key and vertex count, plus the mesh vertex count, a "
            "topology_consistent flag and the modifier list. Check topology_consistent (and whether a "
            "Geometry Nodes modifier is present) before editing any key."
        ),
    )
    def blender_inspect_shape_keys(object: str) -> dict:
        return call(bridge, "inspect_shape_keys", {"object": object})

    @mcp.tool(
        name="blender_create_shape_key",
        description=(
            "Create a shape key on a mesh object. Blender creates the 'Basis' key automatically the first "
            "time, and every new key starts as a copy of the current base mesh. Optional slider_min/max "
            "set the influence range. The mesh topology is validated first; an object with an empty mesh, "
            "or key blocks whose vertex count no longer matches the mesh, is rejected instead of corrupted."
        ),
    )
    def blender_create_shape_key(
        object: str,
        name: str,
        value: float = 0.0,
        from_mix: bool = False,
        slider_min: float | None = None,
        slider_max: float | None = None,
        set_active: bool = False,
    ) -> dict:
        return call(
            bridge,
            "create_shape_key",
            {
                "object": object,
                "name": name,
                "value": value,
                "from_mix": from_mix,
                "slider_min": slider_min,
                "slider_max": slider_max,
                "set_active": set_active,
            },
        )

    @mcp.tool(
        name="blender_set_shape_key_coordinates",
        description=(
            "Write shape key geometry. Provide EITHER 'coordinates' (absolute local [x, y, z] per vertex) "
            "OR 'deltas' (added to the current values). Without 'indices' the list must have exactly one "
            "entry per vertex, otherwise the call fails with a topology_mismatch error naming both counts - "
            "the vertex count of a key cannot be changed. Use 'indices' to touch a subset. 'Basis' is "
            "write-protected unless allow_basis=true."
        ),
    )
    def blender_set_shape_key_coordinates(
        object: str,
        key_name: str,
        coordinates: list[list[float]] | None = None,
        deltas: list[list[float]] | None = None,
        indices: list[int] | None = None,
        allow_basis: bool = False,
    ) -> dict:
        return call(
            bridge,
            "set_shape_key_coordinates",
            {
                "object": object,
                "key_name": key_name,
                "coordinates": coordinates,
                "deltas": deltas,
                "indices": indices,
                "allow_basis": allow_basis,
            },
        )

    @mcp.tool(
        name="blender_set_shape_key",
        description=(
            "Set a shape key's influence value (and optionally mute or the slider range), then return the "
            "re-evaluated mesh statistics and signature. Shape key influence is applied when the "
            "dependency graph evaluates, so the returned evaluated bounds are the real result of the "
            "change. clamp_to_slider=true keeps the value inside the slider range instead of exceeding it."
        ),
    )
    def blender_set_shape_key(
        object: str,
        key_name: str,
        value: float | None = None,
        mute: bool | None = None,
        slider_min: float | None = None,
        slider_max: float | None = None,
        clamp_to_slider: bool = False,
        set_active: bool = False,
    ) -> dict:
        return call(
            bridge,
            "set_shape_key",
            {
                "object": object,
                "key_name": key_name,
                "value": value,
                "mute": mute,
                "slider_min": slider_min,
                "slider_max": slider_max,
                "clamp_to_slider": clamp_to_slider,
                "set_active": set_active,
            },
        )

    @mcp.tool(
        name="blender_remove_shape_key",
        description=(
            "Remove one shape key by name and report the remaining keys. The last remaining key and the "
            "'Basis' key are protected; removing them is refused so the mesh cannot be left in an "
            "inconsistent state."
        ),
    )
    def blender_remove_shape_key(object: str, key_name: str) -> dict:
        return call(bridge, "remove_shape_key", {"object": object, "key_name": key_name})
