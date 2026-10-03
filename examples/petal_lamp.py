#!/usr/bin/env python
"""Petal lamp: one bounded MCP-client recipe (see docs/PETAL_LAMP.md).
Builds a simple biomorphic lamp with Geometry Nodes through the official MCP SDK
(``ClientSession`` + ``stdio_client``) driving this workspace's ``server.py``. Every mutation
is a typed ``blender_*`` tool call: no ``bpy`` import, no raw bridge, no arbitrary code in
Blender, nothing deleted or replaced (an existing node group or object stops the phase).
Sockets are the native Blender 5.2 names of this build (Math operands by index, both display
as "Value"), so a wrong name fails the transactional batch loudly.
Phases, one per invocation: stem -> flower -> verify -> save. Run from the MCP workspace root
with Blender listening on BLENDER_MCP_PORT (default 9876)::
    .venv\\Scripts\\python.exe examples\\petal_lamp.py --phase stem
"""
from __future__ import annotations

import argparse, asyncio, json, math, os, sys
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

GROUP, OBJECT, MODIFIER = "GN_Petal_Lamp", "Petal_Lamp", "Petal Lamp"
ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SAVE_PATH, PREVIEW_PATH = "models/petal_lamp.blend", "previews/petal_lamp.png"
PORT = int(os.environ.get("BLENDER_MCP_PORT") or 9876)
BATCH_LIMIT, PREVIEW_SIZE, PREVIEW_TIMEOUT = 100, 960, 180.0
BOUNDS_TOL, RESTORE_TOL, TWO_PI = 0.010, 0.005, 2.0 * math.pi
PHASES = ("stem", "flower", "verify", "save")
PARAMS = (  # typed interface: (name, default, min, max, subtype, doc); Petal Count is an int socket
    ("Height", 1.10, 0.6, 1.8, "DISTANCE", "Stem height at the tip, metres"),
    ("Stem Bend", 0.22, 0.0, 0.5, "DISTANCE", "Lateral X offset of the stem tip, metres"),
    ("Petal Count", 7, 4, 12, None, "Number of petals around the head"),
    ("Petal Length", 0.22, 0.12, 0.35, "DISTANCE", "Ellipsoid petal half length, metres"),
    ("Opening", 0.65, 0.2, 1.2, "ANGLE", "Petal opening from vertical, radians; larger values spread petals outward"),
    ("Base Radius", 0.20, 0.12, 0.35, "DISTANCE", "Radius of the flattened oval base, metres"))
IFACE = [{"name": n, "socket_type": "NodeSocketInt" if n == "Petal Count" else "NodeSocketFloat", "in_out": "INPUT",
          "default_value": d, "min_value": lo, "max_value": hi, "description": doc, **({"subtype": st} if st else {})}
         for n, d, lo, hi, st, doc in PARAMS] + [{"name": "Geometry", "socket_type": "NodeSocketGeometry", "in_out": "OUTPUT"}]
# native node types (Curve Circle mode=RADIUS, Mesh Circle fill_type=NONE)
UVSPHERE, BEZIER, CIRCLE, CURVE2MESH = ("GeometryNodeMeshUVSphere", "GeometryNodeCurvePrimitiveBezierSegment", "GeometryNodeCurvePrimitiveCircle", "GeometryNodeCurveToMesh")
MESHCIRCLE, INSTANCE, INDEX, REALIZE = ("GeometryNodeMeshCircle", "GeometryNodeInstanceOnPoints", "GeometryNodeInputIndex", "GeometryNodeRealizeInstances")
TRANSFORM, JOIN, MATH, COMBINE = ("GeometryNodeTransform", "GeometryNodeJoinGeometry", "ShaderNodeMath", "ShaderNodeCombineXYZ")
FLOWER_MARKERS, SESSION, CALLS = ("Petal_Instance", "Bulb_Transform"), None, []

# Phase graphs as node tables (name, type, settings, inputs): "@Param" is a group input, "Node:Socket"
# is a link, anything else a socket default. Creates always precede defaults, then links.
STEM = (
    ("Join_Parts", JOIN, None, {}), ("Shared_Sphere", UVSPHERE, None, {"Segments": 32, "Rings": 16, "Radius": 1.0}),
    ("Base_Center", COMBINE, None, {"Z": 0.045}), ("Base_Scale", COMBINE, None, {"X": "@Base Radius", "Y": "@Base Radius", "Z": 0.045}),
    ("Base_Transform", TRANSFORM, None, {"Geometry": "Shared_Sphere:Mesh",  # flattened oval base, on z = 0
        "Translation": "Base_Center:Vector", "Scale": "Base_Scale:Vector"}),
    ("Handle_Height_06", MATH, {"operation": "MULTIPLY"}, {0: "@Height", 1: 0.6}),
    ("Handle_Height_08", MATH, {"operation": "MULTIPLY"}, {0: "@Height", 1: 0.8}),
    ("Start_Handle", COMBINE, None, {"Z": "Handle_Height_06:Value"}), ("End_Handle", COMBINE, None, {"X": "@Stem Bend", "Z": "Handle_Height_08:Value"}),
    ("Stem_Tip", COMBINE, None, {"X": "@Stem Bend", "Z": "@Height"}),  # bent rod ends at the stem tip
    ("Stem_Curve", BEZIER, {"mode": "POSITION"}, {"Start": [0.0, 0.0, 0.08],
        "Start Handle": "Start_Handle:Vector", "End Handle": "End_Handle:Vector", "End": "Stem_Tip:Vector"}),
    ("Stem_Profile", CIRCLE, {"mode": "RADIUS"}, {"Resolution": 12, "Radius": 0.015}),
    ("Stem_Mesh", CURVE2MESH, None, {"Curve": "Stem_Curve:Curve", "Profile Curve": "Stem_Profile:Curve", "Fill Caps": True}),
)
FLOWER = (
    ("Head_Center", COMBINE, None, {"X": "@Stem Bend", "Z": "@Height"}), ("Petal_Rings", MESHCIRCLE, {"fill_type": "NONE"}, {"Vertices": "@Petal Count", "Radius": 0.06}),
    ("Rings_Transform", TRANSFORM, None, {"Geometry": "Petal_Rings:Mesh", "Translation": "Head_Center:Vector"}),  # ring points = instancing source
    ("Petal_Lift", MATH, {"operation": "MULTIPLY"}, {0: "@Petal Length", 1: 0.75}), ("Petal_Center", COMBINE, None, {"Y": "Petal_Lift:Value"}),
    ("Petal_Scale", COMBINE, None, {"X": 0.060, "Y": "@Petal Length", "Z": 0.022}),  # petal half length = Petal Length
    ("Petal_Transform", TRANSFORM, None, {"Geometry": "Shared_Sphere:Mesh", "Translation": "Petal_Center:Vector", "Scale": "Petal_Scale:Vector"}),
    ("Petal_Index", INDEX, None, {}), ("Angle_Step", MATH, {"operation": "DIVIDE"}, {0: TWO_PI, 1: "@Petal Count"}),
    ("Angle_Spread", MATH, {"operation": "MULTIPLY"}, {0: "Angle_Step:Value", 1: "Petal_Index:Index"}),
    ("Petal_Theta", MATH, {"operation": "SUBTRACT"}, {0: "Angle_Spread:Value", 1: math.pi / 2.0}),  # theta = step*Index - pi/2
    ("Petal_Tilt", MATH, {"operation": "SUBTRACT"}, {0: math.pi / 2.0, 1: "@Opening"}),
    ("Petal_Rotation", COMBINE, None, {"X": "Petal_Tilt:Value", "Z": "Petal_Theta:Value"}),
    ("Petal_Instance", INSTANCE, None, {"Points": "Rings_Transform:Geometry", "Instance": "Petal_Transform:Geometry", "Rotation": "Petal_Rotation:Vector"}),
    ("Petal_Realize", REALIZE, None, {"Geometry": "Petal_Instance:Instances"}),
    ("Bulb_Scale", COMBINE, None, {"X": 0.065, "Y": 0.065, "Z": 0.065}),
    ("Bulb_Transform", TRANSFORM, None, {"Geometry": "Shared_Sphere:Mesh", "Translation": "Head_Center:Vector", "Scale": "Bulb_Scale:Vector"}),  # bulb on the stem tip
)


class RecipeError(RuntimeError):
    """A bounded failure the recipe reports as a non-zero exit."""
def log(message): print(message, flush=True)
def parse(result):
    """JSON payload of a CallToolResult (text JSON wins over structured content)."""
    for block in reversed(getattr(result, "content", None) or []):
        try: decoded = json.loads(getattr(block, "text", ""))
        except (ValueError, TypeError): continue
        if isinstance(decoded, dict): return decoded
    return {}
async def call(tool, arguments=None, timeout=None, quiet=False):
    """One typed MCP tool call; raises RecipeError on isError (nothing is retried blind)."""
    result = await SESSION.call_tool(tool, dict(arguments or {}), read_timeout_seconds=timedelta(seconds=float(timeout or 60.0)))
    CALLS.append(tool)
    if getattr(result, "isError", False):
        text = " ".join(b.text for b in (getattr(result, "content", None) or []) if getattr(b, "type", None) == "text")
        raise RecipeError(f"{tool} failed: {(text.strip() or 'no text payload')[:400]}")
    if not quiet: log(f"[ok] {tool}")
    return parse(result)
async def info(): return await call("blender_object_info", {"name": OBJECT}, quiet=True)
async def grp():
    """Live group: nodes, links, interface input identifiers, Group Input/Output node names."""
    p = await call("blender_inspect_node_group", {"name": GROUP}, quiet=True)
    nodes = {n["name"]: n for n in p.get("nodes") or []}
    ends = {k: next((n for n, d in nodes.items() if d.get("bl_idname") == v), None) for k, v in (("in", "NodeGroupInput"), ("out", "NodeGroupOutput"))}
    if None in ends.values(): raise RecipeError(f"group {GROUP!r} has no Group Input/Output node")
    return {"nodes": nodes, "links": p.get("links") or [], "iface": {i["name"]: i["identifier"] for i in (p.get("interface") or {}).get("inputs") or []}, **ends}
def src(state, display_name):
    """(node, socket) of the Group Input output for one interface input, by identifier."""
    ident = state["iface"].get(display_name)
    if ident is None: raise RecipeError(f"group {GROUP!r} has no interface input {display_name!r}")
    return (state["in"], ident)


class Graph:
    """One phase of graph edits: creates -> defaults -> links, <= BATCH_LIMIT per batch."""
    def __init__(self, existing): self.used, self.creates, self.defaults, self.links = set(existing), [], [], []
    def node(self, node_type, name, settings=None, inputs=None):
        """Declare one node; an input that is a (node, socket) pair becomes a link."""
        if name in self.used: raise RecipeError(f"node {name!r} already exists in group {GROUP!r}")
        self.used.add(name)
        self.creates.append({"op": "create_node", "node_type": node_type, "name": name} | ({"settings": settings} if settings else {}))
        for socket, value in (inputs or {}).items():
            if value is None: continue
            self.link(value, name, socket) if isinstance(value, tuple) else self.set(name, socket, value)
    def set(self, node, socket, value):
        self.defaults.append({"op": "set_socket_default", "node": node, "socket": socket, "value": value, "in_out": "INPUT"})
    def link(self, from_, node, socket):
        self.links.append({"op": "link_sockets", "from_node": from_[0], "from_socket": from_[1], "to_node": node, "to_socket": socket, "replace": True})
    def join(self, part): self.link(part, "Join_Parts", "Geometry")  # multi-input socket
def build(state, table):
    """Node table -> graph operations ("@Param" group inputs, "Node:Socket" links)."""
    def value(v):
        if isinstance(v, str) and v.startswith("@"): return src(state, v[1:])
        return tuple(v.split(":", 1)) if isinstance(v, str) and ":" in v else v
    graph = Graph(state["nodes"])
    for name, node_type, settings, inputs in table:
        graph.node(node_type, name, settings, {k: value(v) for k, v in inputs.items()})
    return graph
async def apply(graph, phase):
    """Send one phase as <= BATCH_LIMIT transactional batches (any failure rolls back)."""
    ops = graph.creates + graph.defaults + graph.links
    if not ops: raise RecipeError("internal: the phase produced no graph operations")
    chunks = [ops[i:i + BATCH_LIMIT] for i in range(0, len(ops), BATCH_LIMIT)]
    log(f"[batch] {phase}: {len(graph.creates)} creates, {len(graph.defaults)} defaults, {len(graph.links)} links in {len(chunks)} transactional batch(es)")
    for chunk in chunks:
        await call("blender_batch_graph_edit", {"group": GROUP, "operations": chunk, "transactional": True}, quiet=True)
    want = 2 if phase == "stem" else 4
    parts = [link for link in (await grp())["links"] if link.get("to_node") == "Join_Parts" and link.get("to_socket") == "Geometry"]
    if len(parts) != want:
        raise RecipeError(f"the join node holds {len(parts)} link(s) after phase {phase!r}, expected {want}: its multi-input Geometry socket did not accumulate the parts")
    log(f"[check] join multi-input links after {phase!r}: {len(parts)}")
def modifier_parameters(entry):
    for modifier in entry.get("modifiers") or []:
        if modifier.get("node_group") == GROUP: return dict(modifier.get("parameters") or {})
    raise RecipeError(f"{OBJECT!r} has no Geometry Nodes modifier using group {GROUP!r}")


async def phase_stem():
    summary = await call("blender_scene_summary", {"include_objects": True}, quiet=True)
    existing = {g.get("name") for g in summary.get("node_groups") or []} | {o.get("name") for o in summary.get("objects") or []}
    if GROUP in existing or OBJECT in existing: raise RecipeError(f"{GROUP!r} or {OBJECT!r} already exists: refusing to replace it")
    await call("blender_create_node_group", {"name": GROUP, "interface": IFACE, "if_exists": "error"})
    state = await grp()
    graph = build(state, STEM)
    graph.join(("Base_Transform", "Geometry"))  # oval base + bent rod, joined into Group Output
    graph.join(("Stem_Mesh", "Mesh"))
    graph.link(("Join_Parts", "Geometry"), state["out"], "Geometry")
    await apply(graph, "stem")
    await call("blender_create_mesh_object", {"name": OBJECT, "vertices": [[0.0, 0.0, 0.0]], "faces": [], "location": [1.2, 0.0, 0.0]})
    attach = await call("blender_attach_geometry_nodes_modifier", {"object": OBJECT, "group": GROUP, "modifier_name": MODIFIER, "parameters": {n: d for n, d, *_ in PARAMS}})
    if attach.get("parameters_failed"): raise RecipeError(f"modifier attached but parameters refused: {attach['parameters_failed']}")
    log("[stem] carrier mesh = 1 vertex, no faces: the lamp is generated by the node group only; object at [1.2, 0, 0]")

async def phase_flower():
    state = await grp()
    if all(name in state["nodes"] for name in FLOWER_MARKERS):
        log(f"[skip] phase 'flower' is already built ({FLOWER_MARKERS}); nothing appended")
        return
    graph = build(state, FLOWER)
    graph.join(("Petal_Realize", "Geometry"))  # radial petal ring + central bulb
    graph.join(("Bulb_Transform", "Geometry"))
    graph.node("GeometryNodeSetShadeSmooth", "Smooth_Surface", {"domain": "FACE"},
               {"Geometry": ("Join_Parts", "Geometry"), "Shade Smooth": True})
    graph.link(("Smooth_Surface", "Geometry"), state["out"], "Geometry")
    await apply(graph, "flower")
    await layout_group()

async def layout_group():
    state = await grp()
    parents = {name: set() for name in state["nodes"]}
    for link in state["links"]:
        parents[link["to_node"]].add(link["from_node"])
    levels, remaining = {}, set(parents)
    while remaining:
        ready = [n for n in remaining if parents[n].issubset(levels)]
        if not ready: raise RecipeError("Unexpected graph cycle during layout")
        for name in ready:
            levels[name] = 1 + max((levels[p] for p in parents[name]), default=-1)
            remaining.remove(name)
    rows, operations = {}, []
    for name in state["nodes"]:
        column = levels[name]
        row = rows.get(column, 0)
        rows[column] = row + 1
        operations.append({"op":"set_node_property", "node":name, "property":"location",
                           "value":[column*310-1500, -row*250]})
    await call("blender_batch_graph_edit", {"group":GROUP, "operations":operations,
                                           "transactional":True}, quiet=True)

def set_input(identifier, value):
    return call("blender_set_modifier_input", {"object": OBJECT, "input": identifier, "value": value, "modifier": MODIFIER, "include_evaluated": True})
def measure(entry, key):
    """One evaluated-mesh fact from blender_object_info."""
    if key == "checksum": return (entry.get("evaluated_signature") or {}).get("checksum")
    mesh = entry.get("mesh") or {}
    return (mesh.get("evaluated") or {}).get("vertices") if key == "vertices" else float(((mesh.get("bounds") or {}).get("max") or [0.0, 0.0, 0.0])[2])
def reacted(key, observed, base, delta):
    """Did the evaluated model react the way the graph's arithmetic predicts?"""
    if key == "vertices": return observed > base, f"vertices {base} -> {observed} (must increase)"
    if key == "checksum": return observed != base, f"checksum {base} -> {observed} (must change)"
    return abs((float(observed) - float(base)) - delta) <= BOUNDS_TOL, f"max_z {base:.4f} -> {observed:.4f} (want {delta:+.4f})"

async def phase_verify():
    entry = await info()
    state = await grp()
    ids = {n: src(state, n)[1] for n, *_ in PARAMS}
    values = {n: (modifier_parameters(entry).get(ids[n]) or {}).get("value") for n, *_ in PARAMS}
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values.values()):
        raise RecipeError(f"cannot read every parameter from the modifier: {values}")
    base = {key: measure(entry, key) for key in ("vertices", "max_z", "checksum")}
    log(f"[verify] baseline values={values} {base}")
    checks = []
    try:
        for name, target, key in (("Petal Count", 9, "vertices"), ("Height", 1.35, "max_z"), ("Opening", 0.95, "checksum")):
            await set_input(ids[name], target)
            ok, detail = reacted(key, measure(await info(), key), base[key], float(target) - float(values[name]))
            checks.append((f"{name} {values[name]} -> {target} reacts in the evaluated mesh", ok, detail))
            await set_input(ids[name], values[name])
            back = measure(await info(), key)
            restored = back == base[key] if key in ("vertices", "checksum") else abs(back - base[key]) <= RESTORE_TOL
            checks.append((f"{name} restored to {values[name]}", restored, f"{key}={back}"))
    finally:  # never leave a verification parameter behind, not even after a failed check
        fresh = modifier_parameters(await info())
        for name, value in values.items():
            if (fresh.get(ids[name]) or {}).get("value") != value:
                log(f"[warn] {name!r} was still modified; restoring {value}")
                await call("blender_set_modifier_input", {"object": OBJECT, "input": ids[name], "value": value, "modifier": MODIFIER, "include_evaluated": False}, quiet=True)
    for label, ok, detail in checks: log(f"[verify] {'OK  ' if ok else 'FAIL'} {label}: {detail}")
    failed = [label for label, ok, _ in checks if not ok]
    if failed: raise RecipeError(f"verification failed: {failed}; the graph did not produce the geometry this recipe predicts")
    log(f"[verify] all {len(checks)} checks passed; every parameter is back at its original value")
    await capture_preview()

async def capture_preview():
    try:
        payload = await call("blender_capture_preview", {"object": OBJECT, "path": PREVIEW_PATH,
            "mode": "render", "view": "isometric", "width": PREVIEW_SIZE, "height": PREVIEW_SIZE,
            "timeout": PREVIEW_TIMEOUT, "overwrite": False}, timeout=PREVIEW_TIMEOUT + 60.0, quiet=True)
    except RecipeError as exc:
        if "already exists" not in str(exc): raise
        log(f"[warn] preview {PREVIEW_PATH!r} already exists; left untouched (this recipe never overwrites)")
        return
    size = payload.get("size") or {}
    log(f"[preview] {size.get('width')}x{size.get('height')} -> {payload.get('relative_path')!r} "
        f"({payload.get('bytes')} bytes); the PNG arrives as MCP image content, metadata only here")
async def phase_save():
    payload = await call("blender_save_blend", {"relative_path": SAVE_PATH, "overwrite": False, "compress": False})
    log(f"[save] {payload.get('relative_path')!r} ({payload.get('bytes')} bytes, "
        f"overwritten={payload.get('overwritten')}); no .blend1 backup, no preview, nothing else written")

PHASE_HANDLERS = {"stem": phase_stem, "flower": phase_flower, "verify": phase_verify, "save": phase_save}

def build_parser():
    parser = argparse.ArgumentParser(prog="petal_lamp.py", description=__doc__.strip().splitlines()[0],
        epilog="Phases in order: stem -> flower -> verify -> save; nothing is ever deleted or replaced.")
    parser.add_argument("--phase", required=True, choices=PHASES, help="phase to run")
    parser.add_argument("--port", type=int, default=PORT, help=f"BLENDER_MCP_PORT (default {PORT})")
    return parser
async def run(args):
    global SESSION
    if not VENV_PY.is_file(): raise RecipeError(f"the project venv interpreter {VENV_PY} is missing")
    if not SERVER.is_file(): raise RecipeError(f"MCP server entry point {SERVER} does not exist")
    parameters = StdioServerParameters(command=str(VENV_PY), args=[str(SERVER)], cwd=str(ROOT),
        env={"BLENDER_MCP_PORT": str(int(args.port)), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    log(f"[start] {SERVER} via {VENV_PY} (BLENDER_MCP_PORT={parameters.env['BLENDER_MCP_PORT']})")
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                SESSION = session
                health = await call("blender_health", quiet=True)
                log(f"[{args.phase}] group={GROUP!r} object={OBJECT!r} port={args.port} blender={health.get('blender_version')}")
                await PHASE_HANDLERS[args.phase]()
                log(f"[done] phase {args.phase!r}: {len(CALLS)} MCP call(s) made")
    except PermissionError as exc:
        raise RecipeError(f"could not start the MCP server process (stdio pipes denied: {exc})") from exc
def main(argv=None):
    try:
        asyncio.run(run(build_parser().parse_args(argv)))
    except RecipeError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 2
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
