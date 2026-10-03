"""Tool registration and the FastMCP application factory."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..bridge import BridgeClient
from . import drivers, geometry_nodes, launcher, meta, scene, shape_keys

INSTRUCTIONS = """\
Prefer editable Geometry Nodes for parametric modeling; inspect real sockets before linking,
expose useful group inputs, and verify evaluated geometry after changing parameters. Support
shape keys on original mesh topology and drivers with explicit data paths. Call blender_health
first; if unavailable, call blender_launch_blender. Keep node groups and modifiers editable.
Blender MCP server (stdio). It drives a **running** Blender through a local, token-authenticated
JSON bridge on 127.0.0.1 (default port 9876). Nothing here executes arbitrary Python in Blender:
every capability is a typed tool with explicit validation, and driver expressions are the only
expression channel (they are Blender driver expressions, not Python eval).

Workflow contract
-----------------
1. Call `blender_health` first. It returns the exact Blender version, the loaded .blend, the scene
   and the command list. If it fails, no bridge is running: start Blender with `blender_launch_blender`
   (or run `MCP/blender/bootstrap.py` inside an open Blender) before retrying.
2. Prefer **procedural geometry nodes** over destructive edits. Keep existing modifiers intact;
   adding a Geometry Nodes modifier is additive, and parameters stay editable.
3. **Inspect before you link.** Node socket display names are localized and version dependent;
   identifiers are the stable handle. Use `blender_inspect_node_group` on existing graphs and
   `blender_describe_node_type` / `blender_list_node_types` for runtime discovery of what this
   Blender build actually provides (do not assume a node or socket name exists).
4. Build graphs with `blender_batch_graph_edit` when you have more than one edit. It is **not
   transactional**: read `completed` and `failures` and repair explicitly. Never assume a partial
   batch was rolled back.
5. Verify with data, not with hope: `blender_object_info`, and the `evaluated` /
   `signature` fields returned by modifier and shape key tools, report the evaluated mesh counts and
   world-space bounds produced by the dependency graph.
6. Shape keys require the object's **original mesh topology**: one coordinate per original vertex.
   Basis is created automatically and protected. A Geometry Nodes graph using the original geometry
   can preserve shape-key deformation; geometry generated independently from primitives may ignore
   it. Choose the geometry flow accordingly and verify the result.
7. Drivers evaluate on frame changes and dependency-graph updates. After changing a driver's inputs
   call `blender_set_frame` (to a different frame) or `blender_update_depsgraph`, then read the value.
   Owners supported: object, shape_keys, node_group, scene. `data_path` values are resolved with
   Blender's `ID.path_resolve`, never evaluated as code.
8. Saving is restricted to `<workspace>/Project`. Absolute paths, `..` escapes and symlink/junction
   components are rejected. `blender_save_blend` refuses to overwrite unless `overwrite=true`.
9. Paths, topology, socket compatibility and driver data paths are validated before mutation; a
   failure is reported as a tool error (isError) with a machine-readable code, never as a success
   payload containing "error".
"""


def build_server(bridge: BridgeClient | None = None, name: str = "blender") -> FastMCP:
    """Create the FastMCP app with every tool registered."""
    client = bridge or BridgeClient()
    mcp = FastMCP(name=name, instructions=INSTRUCTIONS)
    meta.register(mcp, client)
    scene.register(mcp, client)
    geometry_nodes.register(mcp, client)
    shape_keys.register(mcp, client)
    drivers.register(mcp, client)
    launcher.register(mcp, client)
    return mcp
