"""Bounded Geometry Nodes canvas layout and native Reroute surgery.

Five named bridge commands:

===================================  ============================================
``geometry_nodes_layout``            read-only structural report of one *exact*
                                     ``GeometryNodeTree``
``geometry_nodes_move_nodes``        move explicitly named nodes
``geometry_nodes_reroute_insert``    insert native ``NodeReroute`` nodes into
                                     existing links
``geometry_nodes_reroute_move``      move explicitly named reroutes
``geometry_nodes_reroute_delete``    delete named reroutes, reconnecting each
                                     source to every downstream target
===================================  ============================================

Guarantees
----------
* **Exact-only resolution.** One node group by exact name, one modifier by exact
  name. Ambiguity (two groups with the same name, several Geometry Nodes
  modifiers and no ``modifier`` argument), linked/override datablocks and a
  group/modifier mismatch are refused with a structured code. Only
  ``GeometryNodeTree`` is accepted - never a shader/compositor/texture tree.
* **Atomic topology edits.** Before the first write the full link list
  (endpoints, order and multi-input sort id), every node's name, parent and
  location are captured. Any failure restores that snapshot **on the same
  datablocks**: the node tree is never copied or swapped in, so object
  modifiers, nested ``GeometryNodeGroup`` nodes, drivers, Frames and every other
  user keep pointing at the very same tree.
* **Semantic verification.** Every mutating command compares a *reroute-blind*
  signature before and after: the sorted resolved endpoints of every
  non-reroute input socket plus the ordered resolved sources of every
  multi-input socket. Inserting or deleting a reroute therefore has to be
  behaviour-preserving, and a mismatch rolls the whole call back.
* **Bounded input.** <=128 links/points per call, finite canvas coordinates,
  duplicate/ambiguous/unknown names refused, bounded name generation that never
  renames another node, and nothing outside the requested names is touched.
* No optional add-on is imported here; only ``bpy`` (and ``math``).

Multi-input ordering
--------------------
A multi-input socket (``NodeSocket.is_multi_input``) needs its link order to
survive the edit. The order is read and written through the first data API this
Blender build actually offers, in this order:

1. ``NodeLink.multi_input_sort_id`` (current RNA name; the pre-rename
   identifier ``multi_input_socket_index`` is accepted as well),
2. ``NodeLink.swap_multi_input_sort_id(other)``,
3. recreating that socket's links in the wanted order with ``NodeLinks.new``.

The method that was really used is reported as ``multi_input_order_method`` and
the order is read back afterwards. If none of the three exists, a call that
would affect a multi-input socket is refused **before** writing anything.

Live-verification status is recorded in ``docs/GEOMETRY_NODES_EDIT.md``: this
module never claims a live Blender result it did not observe.
"""

from __future__ import annotations

import math

import bpy

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
REROUTE_BL_IDNAME = "NodeReroute"
FRAME_BL_IDNAME = "NodeFrame"
GROUP_INPUT_BL_IDNAME = "NodeGroupInput"
GROUP_OUTPUT_BL_IDNAME = "NodeGroupOutput"
GEOMETRY_TREE_IDNAME = "GeometryNodeTree"

#: RNA names for the multi-input ordering index, newest first.
MULTI_INPUT_ID_ATTRS = ("multi_input_sort_id", "multi_input_socket_index")

MAX_BATCH_LINKS = 128
MAX_BATCH_POINTS = 128
MAX_BATCH_NODES = 512
MAX_REPORT_NODES = 2000
MAX_REPORT_LINKS = 5000
MAX_REPORT_LINK_ROWS = 400
MAX_REPORT_INTERFACE = 500
MAX_SNAPSHOT_NODES = 4000
MAX_SNAPSHOT_LINKS = 8000
MAX_NAME_CANDIDATES = 4096
COORD_LIMIT = 1.0e6
REROUTE_BASE_NAME = "Reroute"
MAX_DIFF_ITEMS = 20


# --------------------------------------------------------------------------- #
# pure helpers: no bpy, no api - directly unit-testable
# --------------------------------------------------------------------------- #
def is_int(value) -> bool:
    """True for a real integer (never for a bool, which subclasses int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def link_key(row) -> tuple:
    return (row["from_node"], row["from_socket"], row["to_node"], row["to_socket"])


def socket_key(node, socket) -> str:
    return "%s.%s" % (node, socket)


def endpoint_key(node, socket) -> str:
    return "%s.%s" % (node, socket)


def node_idname(graph, name):
    for node in graph["nodes"]:
        if node["name"] == name:
            return node.get("bl_idname")
    return None


def reroute_set(graph) -> set:
    return {node["name"] for node in graph["nodes"] if node.get("bl_idname") == REROUTE_BL_IDNAME}


def reroute_input_row(graph, name):
    """The single link that feeds a reroute's input side, or None."""
    for row in graph["links"]:
        if row["to_node"] == name:
            return row
    return None


def reroute_output_rows(graph, name) -> list:
    return [row for row in graph["links"] if row["from_node"] == name]


def ordered_rows(rows) -> list:
    """Rows of one multi-input socket in their real order.

    Sort ids win when every row has one; otherwise the captured link order is
    used, which is the only other ordering Blender exposes.
    """
    rows = list(rows)
    ids = [row.get("multi_input_sort_id") for row in rows]
    if rows and all(is_int(value) for value in ids):
        return sorted(rows, key=lambda row: (row["multi_input_sort_id"], row.get("index", 0)))
    return sorted(rows, key=lambda row: row.get("index", 0))


def multi_input_groups(graph) -> dict:
    """``{"node.socket": [rows]}`` for every multi-input input socket."""
    groups: dict = {}
    for row in graph["links"]:
        if row.get("to_multi_input"):
            groups.setdefault(socket_key(row["to_node"], row["to_socket"]), []).append(row)
    return groups


def resolve_source(graph, reroutes, node, socket):
    """Walk upstream through reroutes.

    Returns ``(status, (node, socket))`` with status ``ok``, ``missing`` (a
    reroute with an unconnected input) or ``cycle``.
    """
    current = (node, socket)
    seen = set()
    while current[0] in reroutes:
        if current[0] in seen:
            return "cycle", None
        seen.add(current[0])
        row = reroute_input_row(graph, current[0])
        if row is None:
            return "missing", None
        current = (row["from_node"], row["from_socket"])
    return "ok", current


def effective_source(graph, name, deleting) -> tuple:
    """Source that replaces a deleted reroute's output.

    Follows the chain upstream through reroutes deleted in the same call, so
    ``R1 -> R2 -> target`` with both deleted reconnects ``R1``'s source straight
    to ``target``.
    """
    current = name
    seen = set()
    while current in deleting:
        if current in seen:
            return "cycle", None
        seen.add(current)
        row = reroute_input_row(graph, current)
        if row is None:
            return "missing", None
        if row["from_node"] not in deleting:
            return "ok", (row["from_node"], row["from_socket"])
        current = row["from_node"]
    return "ok", None


def _source_label(graph, reroutes, row) -> str:
    status, source = resolve_source(graph, reroutes, row["from_node"], row["from_socket"])
    if status == "ok":
        return endpoint_key(source[0], source[1])
    return "<%s:%s>" % (status, row["from_node"])


def effective_link_muted(graph, row, through=None):
    """Preserve a disabled wire while removing only the requested anchors."""
    through = reroute_set(graph) if through is None else through
    muted = bool(row.get("is_muted", False))
    name, seen = row["from_node"], set()
    while name in through and name not in seen:
        seen.add(name)
        incoming = reroute_input_row(graph, name)
        if incoming is None:
            break
        muted = muted or bool(incoming.get("is_muted", False))
        name = incoming["from_node"]
    return muted


def project_graph(graph, drop_nodes, add_links) -> dict:
    """Graph view after deleting ``drop_nodes`` and adding ``add_links``."""
    dropped = set(drop_nodes)
    links = []
    for row in graph["links"]:
        if row["from_node"] in dropped or row["to_node"] in dropped:
            continue
        links.append({
            "from_node": row["from_node"],
            "from_socket": row["from_socket"],
            "to_node": row["to_node"],
            "to_socket": row["to_socket"],
            "multi_input_sort_id": row.get("multi_input_sort_id"),
            "index": len(links),
            "to_multi_input": bool(row.get("to_multi_input")),
            "is_muted": bool(row.get("is_muted", False)),
        })
    for row in add_links:
        links.append({
            "from_node": row["from_node"],
            "from_socket": row["from_socket"],
            "to_node": row["to_node"],
            "to_socket": row["to_socket"],
            "multi_input_sort_id": None,
            "index": len(links),
            "to_multi_input": bool(row.get("to_multi_input")),
            "is_muted": bool(row.get("is_muted", False)),
        })
    return {
        "nodes": [node for node in graph["nodes"] if node["name"] not in dropped],
        "links": links,
    }


def reroute_cycle(graph, reroutes) -> bool:
    """True when the reroute wiring itself is cyclic.

    Walked separately from the edge resolution, because a cycle made only of
    reroutes never produces a resolvable edge and would otherwise go unnoticed.
    """
    for name in reroutes:
        seen = set()
        current = name
        while current in reroutes:
            if current in seen:
                return True
            seen.add(current)
            row = reroute_input_row(graph, current)
            if row is None:
                break
            current = row["from_node"]
    return False


def semantic_signature(graph) -> dict:
    """Reroute-blind behaviour signature of a graph view.

    Reroute nodes are pure wiring, so a link that reaches a real input socket
    through any number of reroutes is reported by its resolved source. The
    ordered resolved sources of every multi-input socket are part of the
    signature, which is what stops "same endpoints, different order" from
    passing as equivalent.
    """
    reroutes = reroute_set(graph)
    edges = []
    muted_edges = []
    dangling = []
    cycle = reroute_cycle(graph, reroutes)
    for row in graph["links"]:
        if row["to_node"] in reroutes:
            continue
        status, source = resolve_source(graph, reroutes, row["from_node"], row["from_socket"])
        if status == "ok":
            edges.append([source[0], source[1], row["to_node"], row["to_socket"]])
            if effective_link_muted(graph, row):
                muted_edges.append([source[0], source[1], row["to_node"], row["to_socket"]])
        else:
            edges.append(["<%s>" % status, row["from_node"], row["to_node"], row["to_socket"]])
            if status == "missing":
                dangling.append(row["from_node"])
            if status == "cycle":
                cycle = True
    edges.sort()
    orders = {}
    for key, rows in multi_input_groups(graph).items():
        orders[key] = [_source_label(graph, reroutes, row) for row in ordered_rows(rows)]
    return {
        "edges": edges,
        "muted_edges": sorted(muted_edges),
        "multi_input_orders": orders,
        "dangling_reroute_inputs": sorted(set(dangling)),
        "reroute_cycle": cycle,
    }


def signature_diff(before: dict, after: dict) -> dict:
    """Bounded, human-readable difference between two signatures."""
    before_edges = {tuple(edge) for edge in before["edges"]}
    after_edges = {tuple(edge) for edge in after["edges"]}
    removed = sorted(before_edges - after_edges)
    added = sorted(after_edges - before_edges)
    changed_orders = {}
    for key in sorted(set(before["multi_input_orders"]) | set(after["multi_input_orders"])):
        old = before["multi_input_orders"].get(key)
        new = after["multi_input_orders"].get(key)
        if old != new:
            changed_orders[key] = {"before": old, "after": new}
    return {
        "removed_edges": [list(edge) for edge in removed[:MAX_DIFF_ITEMS]],
        "added_edges": [list(edge) for edge in added[:MAX_DIFF_ITEMS]],
        "removed_edges_more": max(0, len(removed) - MAX_DIFF_ITEMS),
        "added_edges_more": max(0, len(added) - MAX_DIFF_ITEMS),
        "changed_multi_input_orders": changed_orders,
        "muted_edges": {"before": before.get("muted_edges", []),
                        "after": after.get("muted_edges", [])},
    }


def compare_signatures(before: dict, after: dict) -> dict:
    """Plain comparison of two :func:`semantic_signature` results."""
    edges_equal = before["edges"] == after["edges"]
    orders_equal = before["multi_input_orders"] == after["multi_input_orders"]
    dangling_equal = before["dangling_reroute_inputs"] == after["dangling_reroute_inputs"]
    muted_equal = before.get("muted_edges", []) == after.get("muted_edges", [])
    result = {
        "semantic_endpoints_equal": edges_equal,
        "multi_input_order_equal": orders_equal,
        "dangling_reroute_inputs_equal": dangling_equal,
        "muted_links_equal": muted_equal,
        "reroute_cycle": bool(after["reroute_cycle"]),
        "edges_before": len(before["edges"]),
        "edges_after": len(after["edges"]),
        "multi_input_sockets": sorted(after["multi_input_orders"]),
        "equivalent": bool(edges_equal and orders_equal and dangling_equal and muted_equal
                           and not after["reroute_cycle"]),
    }
    if not result["equivalent"]:
        result["diff"] = signature_diff(before, after)
    return result


def allocate_names(existing, count: int, base: str = REROUTE_BASE_NAME) -> list:
    """Bounded ``Reroute``/``Reroute.001``... names free of ``existing``.

    Returns fewer than ``count`` names when the bounded search is exhausted; the
    caller then refuses the call. No other node is ever renamed.
    """
    if count <= 0:
        return []
    taken = set(existing or ())
    names = []
    for index in range(MAX_NAME_CANDIDATES):
        candidate = base if index == 0 else "%s.%03d" % (base, index)
        if candidate in taken:
            continue
        taken.add(candidate)
        names.append(candidate)
        if len(names) >= count:
            break
    return names


def _location_of(nodes, name):
    for node in nodes:
        if node["name"] == name:
            value = node.get("location")
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
            ):
                return (float(value[0]), float(value[1]))
            return (0.0, 0.0)
    return (0.0, 0.0)


def midpoint_position(nodes, source_name, target_names) -> list:
    """Deterministic default position: mean of source and target node origins."""
    points = [_location_of(nodes, source_name)] + [_location_of(nodes, name) for name in target_names]
    return [
        round(sum(point[0] for point in points) / len(points), 3),
        round(sum(point[1] for point in points) / len(points), 3),
    ]


def _validate_points(points, expected: int):
    """``(resolved_points, problems)`` for an explicit canvas-coordinate list."""
    problems = []
    if not isinstance(points, list):
        return None, [{"code": "invalid_points", "message": "points must be a list of [x, y] pairs"}]
    if len(points) > MAX_BATCH_POINTS:
        return None, [{
            "code": "too_many_points",
            "message": "points has %d entries, the maximum is %d" % (len(points), MAX_BATCH_POINTS),
        }]
    if len(points) != expected:
        return None, [{
            "code": "points_count_mismatch",
            "message": "points has %d entries but %d reroute(s) would be created"
            % (len(points), expected),
        }]
    resolved = []
    for index, value in enumerate(points):
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
                for item in value
            )
        ):
            problems.append({
                "code": "invalid_coordinates",
                "index": index,
                "message": "points[%d] must be two finite numbers" % index,
            })
            continue
        if any(abs(item) > COORD_LIMIT for item in value):
            problems.append({
                "code": "invalid_coordinates",
                "index": index,
                "message": "points[%d] is outside ±%g" % (index, COORD_LIMIT),
            })
            continue
        resolved.append([float(value[0]), float(value[1])])
    return resolved, problems


def plan_problem_message(action: str, problems) -> str:
    """One bounded, explicit sentence for a rejected preflight."""
    head = problems[0] if problems else {}
    text = "%s refused: %s" % (action, head.get("message") or head.get("code") or "invalid request")
    if len(problems) > 1:
        text += " (+%d more problem(s))" % (len(problems) - 1)
    return text


def plan_reroute_insert(graph, specs, points, shared_branch: bool, limit: int = MAX_BATCH_LINKS):
    """Work out every reroute ``geometry_nodes_reroute_insert`` would create.

    Pure: returns ``(plan, problems)``. A non-empty ``problems`` list means the
    caller must refuse the whole call without touching the tree.

    ``shared_branch=false`` inserts one reroute per link. ``shared_branch=true``
    inserts one reroute per distinct source *socket* and fans it out to every
    listed target of that source - two different sources feeding one multi-input
    socket therefore still get two independent reroutes.
    """
    problems = []
    if not isinstance(specs, list) or not specs:
        return None, [{"code": "invalid_links", "message": "links must be a non-empty list"}]
    if len(specs) > limit:
        return None, [{
            "code": "too_many_links",
            "message": "links has %d entries, the maximum is %d" % (len(specs), limit),
        }]
    if not isinstance(shared_branch, bool):
        return None, [{"code": "invalid_argument", "message": "shared_branch must be boolean"}]

    counts: dict = {}
    for row in graph["links"]:
        counts[link_key(row)] = counts.get(link_key(row), 0) + 1

    matched = []
    seen = set()
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            problems.append({
                "code": "invalid_link_spec",
                "index": index,
                "message": "links[%d] must be an object with from_node/from_socket/to_node/to_socket" % index,
            })
            continue
        key = (spec.get("from_node"), spec.get("from_socket"), spec.get("to_node"), spec.get("to_socket"))
        if any(not isinstance(value, str) or not value for value in key):
            problems.append({
                "code": "invalid_link_spec",
                "index": index,
                "message": "links[%d] needs non-empty from_node/from_socket/to_node/to_socket strings" % index,
            })
            continue
        if key in seen:
            problems.append({
                "code": "duplicate_link_spec",
                "index": index,
                "message": "links[%d] repeats an identical endpoint key" % index,
                "link": list(key),
            })
            continue
        seen.add(key)
        found = counts.get(key, 0)
        if found == 0:
            problems.append({
                "code": "link_not_found",
                "index": index,
                "message": "no link from %s.%s to %s.%s in this node tree" % key,
                "link": list(key),
            })
            continue
        if found > 1:
            problems.append({
                "code": "ambiguous_link",
                "index": index,
                "message": "%d identical links from %s.%s to %s.%s exist; refusing to guess" % ((found,) + key),
                "link": list(key),
            })
            continue
        row = next(item for item in graph["links"] if link_key(item) == key)
        hint = spec.get("multi_input_sort_id")
        if hint is not None:
            if not is_int(hint):
                problems.append({
                    "code": "invalid_link_spec",
                    "index": index,
                    "message": "multi_input_sort_id must be an integer or omitted",
                })
                continue
            if row.get("multi_input_sort_id") is None:
                problems.append({
                    "code": "sort_id_unavailable",
                    "index": index,
                    "message": "this Blender build exposes no multi-input sort id, so it cannot be asserted",
                    "link": list(key),
                })
                continue
            if row["multi_input_sort_id"] != hint:
                problems.append({
                    "code": "sort_id_mismatch",
                    "index": index,
                    "message": "link %s.%s -> %s.%s has multi_input_sort_id %s, not %s"
                    % (key + (row["multi_input_sort_id"], hint)),
                    "link": list(key),
                })
                continue
        matched.append((index, row))

    if problems:
        return None, problems

    groups: dict = {}
    order = []
    for index, row in matched:
        group_key = (row["from_node"], row["from_socket"]) if shared_branch else (index,)
        if group_key not in groups:
            groups[group_key] = {"source": (row["from_node"], row["from_socket"]), "targets": [], "specs": []}
            order.append(group_key)
        group = groups[group_key]
        target = (row["to_node"], row["to_socket"])
        if any(item["node"] == target[0] and item["socket"] == target[1] for item in group["targets"]):
            problems.append({
                "code": "duplicate_target",
                "index": index,
                "message": "source %s.%s already has %s.%s as a target in this call; one reroute cannot carry "
                           "the same source into one socket twice"
                % (row["from_node"], row["from_socket"], target[0], target[1]),
                "link": list(link_key(row)),
            })
            continue
        group["targets"].append({
            "node": target[0],
            "socket": target[1],
            "multi_input_sort_id": row.get("multi_input_sort_id"),
            "multi_input": bool(row.get("to_multi_input")),
            "replace": list(link_key(row)),
            "spec_index": index,
        })
        group["specs"].append(index)
    if problems:
        return None, problems

    names = allocate_names({node["name"] for node in graph["nodes"]}, len(order))
    if len(names) != len(order):
        return None, [{
            "code": "name_allocation_failed",
            "message": "could not find %d unused reroute names within %d candidates"
            % (len(order), MAX_NAME_CANDIDATES),
        }]

    resolved_points = None
    positions_source = "midpoint"
    if points is not None:
        resolved_points, point_problems = _validate_points(points, len(order))
        if point_problems:
            return None, point_problems
        positions_source = "explicit"

    reroutes = []
    for position, group_key in enumerate(order):
        group = groups[group_key]
        location = (
            resolved_points[position]
            if resolved_points is not None
            else midpoint_position(graph["nodes"], group["source"][0], [item["node"] for item in group["targets"]])
        )
        reroutes.append({
            "name": names[position],
            "location": location,
            "source": {"node": group["source"][0], "socket": group["source"][1]},
            "targets": group["targets"],
            "spec_indexes": group["specs"],
        })

    # Desired post-edit order of every multi-input socket that loses a link to a
    # new reroute: the current order with each replaced link substituted by the
    # reroute that now carries it.
    replacement = {}
    for position, item in enumerate(reroutes):
        for target in item["targets"]:
            replacement[tuple(target["replace"])] = (position, target["node"], target["socket"])
    order_plan = []
    for key, rows in multi_input_groups(graph).items():
        entries = []
        for row in ordered_rows(rows):
            swap = replacement.get(link_key(row))
            if swap is None:
                entries.append({"kind": "keep", "key": list(link_key(row))})
            else:
                entries.append({"kind": "new", "plan": swap[0], "key": [swap[1], swap[2]]})
        if any(entry["kind"] == "new" for entry in entries):
            order_plan.append({
                "socket": key,
                "node": rows[0]["to_node"],
                "socket_name": rows[0]["to_socket"],
                "entries": entries,
            })

    return {
        "reroutes": reroutes,
        "order": order_plan,
        "positions_source": positions_source,
        "links_before": len(graph["links"]),
        "expected_links_after": len(graph["links"]) + len(reroutes),
        "affected_sockets": [item["socket"] for item in order_plan],
    }, []


def plan_reroute_delete(graph, names, limit: int = MAX_BATCH_NODES):
    """Work out the reconnection for ``geometry_nodes_reroute_delete``.

    A reroute with no input link is refused instead of being deleted silently;
    a delete that would need the same source twice in one multi-input socket is
    refused instead of merging two independent sources.
    """
    problems = []
    if not isinstance(names, list) or not names:
        return None, [{"code": "invalid_nodes", "message": "nodes must be a non-empty list of reroute names"}]
    if len(names) > limit:
        return None, [{
            "code": "too_many_nodes",
            "message": "nodes has %d entries, the maximum is %d" % (len(names), limit),
        }]
    reroutes = reroute_set(graph)
    resolved = []
    seen = set()
    for index, raw in enumerate(names):
        if not isinstance(raw, str) or not raw:
            problems.append({"code": "invalid_name", "index": index, "message": "nodes[%d] must be a non-empty name" % index})
            continue
        if raw in seen:
            continue
        seen.add(raw)
        bl_idname = node_idname(graph, raw)
        if bl_idname is None:
            problems.append({
                "code": "node_not_found",
                "index": index,
                "message": "no node named %r in this node tree" % raw,
                "node": raw,
            })
            continue
        if raw not in reroutes:
            problems.append({
                "code": "wrong_node_type",
                "index": index,
                "message": "node %r is a %s, not a NodeReroute; this command only deletes reroutes"
                % (raw, bl_idname),
                "node": raw,
            })
            continue
        resolved.append(raw)
    if problems:
        return None, problems

    deleting = set(resolved)
    if reroute_cycle(graph, reroutes):
        return None, [{
            "code": "reroute_cycle",
            "message": "this node tree already contains cyclic reroute wiring; refusing to delete through it",
            "nodes": resolved,
        }]
    reconnects = []
    dead_ends = []
    for name in resolved:
        if reroute_input_row(graph, name) is None:
            problems.append({
                "code": "reroute_input_missing",
                "node": name,
                "message": "reroute %r has no input link, so deleting it would silently discard an unconnected "
                           "anchor; refusing. Use blender_remove_node if the bare node really must go." % name,
            })
            continue
        status, source = effective_source(graph, name, deleting)
        if status == "cycle":
            problems.append({
                "code": "reroute_cycle",
                "node": name,
                "message": "the reroute chain feeding %r is cyclic; refusing to rewire it" % name,
            })
            continue
        if status != "ok" or source is None:
            problems.append({
                "code": "reroute_input_missing",
                "node": name,
                "message": "reroute %r has no resolvable source" % name,
            })
            continue
        downstream = reroute_output_rows(graph, name)
        if not downstream:
            dead_ends.append(name)
        for row in downstream:
            reconnects.append({
                "source": {"node": source[0], "socket": source[1]},
                "target": {"node": row["to_node"], "socket": row["to_socket"]},
                "replaces": list(link_key(row)),
                "multi_input": bool(row.get("to_multi_input")),
                "is_muted": effective_link_muted(graph, row, deleting),
            })
    if problems:
        return None, problems

    projected = project_graph(
        graph,
        deleting,
        [
            {
                "from_node": entry["source"]["node"],
                "from_socket": entry["source"]["socket"],
                "to_node": entry["target"]["node"],
                "to_socket": entry["target"]["socket"],
                "to_multi_input": entry["multi_input"],
                "is_muted": entry["is_muted"],
            }
            for entry in reconnects
        ],
    )
    if reroute_cycle(projected, reroute_set(projected)):
        return None, [{
            "code": "reroute_cycle",
            "message": "reconnecting these reroutes' sources would create a cyclic reroute chain; refusing",
            "nodes": resolved,
        }]

    order_plan = []
    for key, rows in sorted(multi_input_groups(graph).items()):
        if not any(row["from_node"] in deleting for row in rows):
            continue
        entries = []
        duplicates = []
        for row in ordered_rows(rows):
            if row["from_node"] in deleting:
                status, source = effective_source(graph, row["from_node"], deleting)
                if status != "ok" or source is None:
                    continue
                entry = {
                    "kind": "new",
                    "key": [source[0], source[1], row["to_node"], row["to_socket"]],
                    "from_reroute": row["from_node"],
                }
            else:
                entry = {"kind": "keep", "key": list(link_key(row))}
            if any(existing["key"] == entry["key"] for existing in entries):
                duplicates.append(entry["key"])
            entries.append(entry)
        if duplicates:
            problems.append({
                "code": "delete_would_merge_sources",
                "socket": key,
                "message": "deleting these reroutes would feed %s from the same source twice; Blender cannot hold "
                           "two identical links, so independent sources would have to be merged. Delete them one "
                           "at a time instead." % key,
                "duplicates": duplicates,
            })
            continue
        order_plan.append({
            "socket": key,
            "node": rows[0]["to_node"],
            "socket_name": rows[0]["to_socket"],
            "entries": entries,
        })
    if problems:
        return None, problems

    returning = [row for row in graph["links"] if row["from_node"] in deleting or row["to_node"] in deleting]
    return {
        "nodes": resolved,
        "reconnects": reconnects,
        "order": order_plan,
        "affected_sockets": [item["socket"] for item in order_plan],
        "dead_ends": dead_ends,
        "links_before": len(graph["links"]),
        "links_removed": len(returning),
        "expected_links_after": len(graph["links"]) - len(returning) + len(reconnects),
    }, []


def plan_move_items(graph, items, reroutes_only: bool, limit: int = MAX_BATCH_NODES):
    """Validate ``positions`` for the two move commands (pure)."""
    problems = []
    if not isinstance(items, list) or not items:
        return None, [{"code": "invalid_positions", "message": "positions must be a non-empty list"}]
    if len(items) > limit:
        return None, [{
            "code": "too_many_nodes",
            "message": "positions has %d entries, the maximum is %d" % (len(items), limit),
        }]
    reroutes = reroute_set(graph)
    plan = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append({"code": "invalid_positions", "index": index, "message": "positions[%d] must be an object" % index})
            continue
        name = item.get("node")
        if not isinstance(name, str) or not name:
            problems.append({
                "code": "invalid_positions",
                "index": index,
                "message": "positions[%d].node must be a non-empty node name" % index,
            })
            continue
        if name in seen:
            problems.append({
                "code": "duplicate_node",
                "index": index,
                "message": "positions[%d] names %r twice" % (index, name),
                "node": name,
            })
            continue
        seen.add(name)
        bl_idname = node_idname(graph, name)
        if bl_idname is None:
            problems.append({
                "code": "node_not_found",
                "index": index,
                "message": "no node named %r in this node tree" % name,
                "node": name,
            })
            continue
        if reroutes_only and name not in reroutes:
            problems.append({
                "code": "wrong_node_type",
                "index": index,
                "message": "node %r is a %s, not a NodeReroute" % (name, bl_idname),
                "node": name,
            })
            continue
        values = [item.get("x"), item.get("y")]
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in values
        ):
            problems.append({
                "code": "invalid_coordinates",
                "index": index,
                "message": "positions[%d] needs finite numeric x and y" % index,
                "node": name,
            })
            continue
        if any(abs(value) > COORD_LIMIT for value in values):
            problems.append({
                "code": "invalid_coordinates",
                "index": index,
                "message": "positions[%d] is outside ±%g" % (index, COORD_LIMIT),
                "node": name,
            })
            continue
        plan.append({"node": name, "x": float(values[0]), "y": float(values[1]), "bl_idname": bl_idname})
    if problems:
        return None, problems
    return plan, []


# --------------------------------------------------------------------------- #
# bpy helpers
# --------------------------------------------------------------------------- #
def _tree_type(tree) -> str:
    identifier = getattr(getattr(tree, "bl_rna", None), "identifier", None)
    return identifier or getattr(tree, "bl_idname", "") or ""


def _socket_collection(node, in_out: str):
    return node.outputs if in_out == "OUTPUT" else node.inputs


def _socket_by_identifier(node, ref, in_out: str, error):
    sockets = _socket_collection(node, in_out)
    found = None
    if is_int(ref):
        if 0 <= ref < len(sockets):
            found = sockets[ref]
    elif isinstance(ref, str) and ref:
        for candidate in sockets:
            if candidate.identifier == ref:
                found = candidate
                break
        if found is None:
            found = sockets.get(ref)
    if found is None:
        raise error(
            "socket_not_found",
            "no %s socket %r on node %r" % (in_out.lower(), ref, node.name),
            details={"node": node.name, "socket": ref, "available": [s.identifier for s in sockets]},
        )
    return found


def _sort_attr(link):
    for attr in MULTI_INPUT_ID_ATTRS:
        try:
            getattr(link, attr)
        except AttributeError:
            continue
        return attr
    return None


def _read_sort_id(link):
    attr = _sort_attr(link)
    if attr is None:
        return None
    value = getattr(link, attr)
    return int(value) if is_int(value) else None


def _is_multi_input(socket) -> bool:
    flag = getattr(socket, "is_multi_input", None)
    if isinstance(flag, bool):
        return flag
    # Older builds do not expose the flag; several links into one input socket is
    # then the only available signal.
    return len(getattr(socket, "links", []) or []) > 1


def _socket_link_objects(socket) -> list:
    return list(getattr(socket, "links", []) or [])


def _ordered_socket_links(socket) -> tuple:
    """``(links_in_order, ids_used)`` for one socket."""
    links = _socket_link_objects(socket)
    ids = [_read_sort_id(link) for link in links]
    if links and all(value is not None for value in ids):
        return [pair[1] for pair in sorted(zip(ids, links), key=lambda pair: pair[0])], True
    return links, False


def _link_key_of(link) -> tuple:
    return (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)


def _lookup_link(tree, key):
    return [link for link in tree.links if _link_key_of(link) == tuple(key)]


def _find_link(tree, key, error):
    matches = _lookup_link(tree, key)
    if len(matches) != 1:
        raise error(
            "link_not_found" if not matches else "ambiguous_link",
            "%d link(s) match %s.%s -> %s.%s; expected exactly one" % ((len(matches),) + tuple(key)),
            details={"link": list(key)},
        )
    return matches[0]


def _socket_or_error(tree, node_name, socket_ref, in_out, error):
    node = tree.nodes.get(node_name)
    if node is None:
        raise error("node_not_found", "node %r disappeared during the edit" % node_name, details={"node": node_name})
    return _socket_by_identifier(node, socket_ref, in_out, error)


def _new_link(tree, from_node, from_socket, to_node, to_socket, error, muted=False):
    source = _socket_or_error(tree, from_node, from_socket, "OUTPUT", error)
    target = _socket_or_error(tree, to_node, to_socket, "INPUT", error)
    try:
        # Same call order as the verified ``link_sockets`` command: (output, input).
        link = tree.links.new(source, target)
        if muted or bool(getattr(link, "is_muted", False)):
            link.is_muted = bool(muted)
        return link
    except (RuntimeError, TypeError, ValueError) as exc:
        raise error(
            "link_failed",
            "Blender rejected the link %s.%s -> %s.%s: %s" % (from_node, from_socket, to_node, to_socket, exc),
            details={"from": {"node": from_node, "socket": from_socket}, "to": {"node": to_node, "socket": to_socket}},
        ) from exc


def _reroute_state(node):
    if getattr(node, "bl_idname", None) != REROUTE_BL_IDNAME:
        return {}
    state = {}
    for name in ("label", "mute", "hide", "select", "width", "use_custom_color", "color",
                 "show_options", "show_preview", "show_texture"):
        if hasattr(node, name):
            value = getattr(node, name)
            state[name] = value if isinstance(value, (str, bool, int, float)) else list(value)
    return state


def capture_graph(tree, error) -> dict:
    """Reroute-aware, bounded snapshot of one tree (also the rollback source)."""
    nodes = []
    for node in tree.nodes:
        nodes.append({
            "name": node.name,
            "bl_idname": getattr(node, "bl_idname", ""),
            "location": [float(value) for value in node.location],
            "parent": node.parent.name if getattr(node, "parent", None) else None,
            "reroute_state": _reroute_state(node),
        })
    if len(nodes) > MAX_SNAPSHOT_NODES:
        raise error(
            "tree_too_large",
            "node tree %r has %d nodes; this bounded command supports at most %d"
            % (tree.name, len(nodes), MAX_SNAPSHOT_NODES),
            details={"nodes": len(nodes), "max_nodes": MAX_SNAPSHOT_NODES},
        )
    links = []
    for index, link in enumerate(tree.links):
        links.append({
            "from_node": link.from_node.name,
            "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name,
            "to_socket": link.to_socket.identifier,
            "multi_input_sort_id": _read_sort_id(link),
            "index": index,
            "to_multi_input": _is_multi_input(link.to_socket),
            "is_muted": bool(getattr(link, "is_muted", False)),
        })
    if len(links) > MAX_SNAPSHOT_LINKS:
        raise error(
            "tree_too_large",
            "node tree %r has %d links; this bounded command supports at most %d"
            % (tree.name, len(links), MAX_SNAPSHOT_LINKS),
            details={"links": len(links), "max_links": MAX_SNAPSHOT_LINKS},
        )
    return {"nodes": nodes, "links": links}


def _graph_view(tree) -> dict:
    """Compact view for the pure planners (names, idnames, links, sort ids)."""
    links = []
    for index, link in enumerate(tree.links):
        links.append({
            "from_node": link.from_node.name,
            "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name,
            "to_socket": link.to_socket.identifier,
            "multi_input_sort_id": _read_sort_id(link),
            "index": index,
            "to_multi_input": _is_multi_input(link.to_socket),
            "is_muted": bool(getattr(link, "is_muted", False)),
        })
    return {
        "nodes": [
            {
                "name": node.name,
                "bl_idname": getattr(node, "bl_idname", ""),
                "location": [float(value) for value in node.location],
                "parent": node.parent.name if getattr(node, "parent", None) else None,
            }
            for node in tree.nodes
        ],
        "links": links,
    }


def _apply_link_order(tree, socket, desired_keys, error) -> dict:
    """Force one multi-input socket's links into ``desired_keys`` order.

    Returns ``{"ok": bool, "method": str, "verified_by": str, ...}``; never
    claims success without reading the order back.
    """
    desired = [tuple(key) for key in desired_keys]
    links, ids_used = _ordered_socket_links(socket)
    current = [_link_key_of(link) for link in links]
    if sorted(current) != sorted(desired):
        return {
            "ok": False,
            "reason": "link_set_mismatch",
            "current": [list(key) for key in current],
            "desired": [list(key) for key in desired],
        }
    if current == desired:
        return {"ok": True, "method": "already_ordered", "verified_by": "sort_id" if ids_used else "link_order"}

    by_key = {_link_key_of(link): link for link in links}
    attr = _sort_attr(links[0]) if links else None
    if attr is not None:
        try:
            for index, key in enumerate(desired):
                setattr(by_key[key], attr, index)
        except (AttributeError, TypeError, RuntimeError):
            pass  # read-only in this build: fall through to the next strategy
        else:
            after, used = _ordered_socket_links(socket)
            if [_link_key_of(link) for link in after] == desired:
                return {"ok": True, "method": "write:" + attr, "verified_by": "sort_id" if used else "link_order"}

    swap = None
    for link in links:
        candidate = getattr(link, "swap_multi_input_sort_id", None)
        if callable(candidate):
            swap = candidate
            break
    if swap is not None:
        for _ in range(len(desired) * len(desired) + 4):
            current_links, _used = _ordered_socket_links(socket)
            current = [_link_key_of(link) for link in current_links]
            if current == desired:
                return {"ok": True, "method": "swap_multi_input_sort_id", "verified_by": "sort_id"}
            moved = False
            for index in range(len(current) - 1):
                if current[index] != desired[index]:
                    target = current.index(desired[index])
                    if target > index:
                        try:
                            current_links[target].swap_multi_input_sort_id(current_links[target - 1])
                        except (AttributeError, TypeError, RuntimeError):
                            moved = False
                        else:
                            moved = True
                    break
            if not moved:
                break

    muted_by_key = {key: bool(getattr(link, "is_muted", False)) for key, link in by_key.items()}
    for link in list(_socket_link_objects(socket)):
        tree.links.remove(link)
    for key in desired:
        _new_link(tree, key[0], key[1], key[2], key[3], error,
                  muted=muted_by_key[key])
    after, used = _ordered_socket_links(socket)
    if [_link_key_of(link) for link in after] == desired:
        return {"ok": True, "method": "recreate_links", "verified_by": "sort_id" if used else "link_order"}
    return {
        "ok": False,
        "reason": "order_not_restored",
        "current": [list(_link_key_of(link)) for link in after],
        "desired": [list(key) for key in desired],
    }


def _order_entries_to_keys(entries, created, error) -> list:
    """Resolve planner order entries to real link keys after an insert."""
    keys = []
    for entry in entries:
        if entry["kind"] == "new":
            plan_index, to_node, to_socket = entry["plan"], entry["key"][0], entry["key"][1]
            node = created[plan_index]
            if not node.outputs:
                raise error("unexpected_reroute_sockets", "reroute %r has no output socket" % node.name)
            keys.append([node.name, node.outputs[0].identifier, to_node, to_socket])
        else:
            keys.append(list(entry["key"]))
    return keys


def restore_graph(tree, snapshot, error) -> dict:
    """Put a captured graph back on the same datablocks, then verify the order."""
    try:
        for link in list(tree.links):
            tree.links.remove(link)
        keep = {row["name"] for row in snapshot["nodes"]}
        for node in list(tree.nodes):
            if node.name not in keep:
                tree.nodes.remove(node)
        # Only the commands in this module remove nodes, and they only ever
        # remove rewritable NodeReroute anchors, so those can be recreated. Any
        # other missing node is reported instead of being silently skipped.
        for row in snapshot["nodes"]:
            if tree.nodes.get(row["name"]) is not None:
                continue
            if row["bl_idname"] != REROUTE_BL_IDNAME:
                raise error(
                    "rollback_failed",
                    "cannot restore node tree %r: node %r (%s) is missing and cannot be recreated"
                    % (tree.name, row["name"], row["bl_idname"]),
                    details={"missing_node": row["name"], "bl_idname": row["bl_idname"]},
                )
            restored = tree.nodes.new(REROUTE_BL_IDNAME)
            restored.name = row["name"]
            if restored.name != row["name"]:
                raise error(
                    "rollback_failed",
                    "cannot restore node tree %r: Blender renamed the recreated reroute to %r"
                    % (tree.name, restored.name),
                    details={"expected": row["name"], "actual": restored.name},
                )
        missing = [row["name"] for row in snapshot["nodes"] if tree.nodes.get(row["name"]) is None]
        if missing:
            raise error(
                "rollback_failed",
                "cannot restore node tree %r: node(s) %s no longer exist" % (tree.name, ", ".join(missing[:8])),
                details={"missing_nodes": missing},
            )
        for row in snapshot["nodes"]:
            parent = row["parent"]
            tree.nodes.get(row["name"]).parent = tree.nodes.get(parent) if parent else None
        for row in snapshot["nodes"]:
            node = tree.nodes.get(row["name"])
            node.location = [row["location"][0], row["location"][1]]
            for name, value in row.get("reroute_state", {}).items():
                setattr(node, name, value)
        for row in snapshot["links"]:
            _new_link(tree, row["from_node"], row["from_socket"], row["to_node"], row["to_socket"], error,
                      muted=row.get("is_muted", False))
    except Exception as exc:  # noqa: BLE001 - a failed rollback is reported, never hidden
        if getattr(exc, "code", None) == "rollback_failed":
            raise
        raise error(
            "rollback_failed",
            "the edit failed AND the original topology could not be restored: %s: %s" % (type(exc).__name__, exc),
            details={"rolled_back": False, "rollback_error": "%s: %s" % (type(exc).__name__, exc)},
        ) from exc

    order_restored, order_failed = [], []
    groups: dict = {}
    for row in snapshot["links"]:
        if row.get("to_multi_input"):
            groups.setdefault(socket_key(row["to_node"], row["to_socket"]), []).append(row)
    for key, rows in groups.items():
        try:
            socket = _socket_or_error(tree, rows[0]["to_node"], rows[0]["to_socket"], "INPUT", error)
        except Exception:  # noqa: BLE001 - reported through order_failed
            order_failed.append(key)
            continue
        desired = [list(link_key(row)) for row in ordered_rows(rows)]
        try:
            outcome = _apply_link_order(tree, socket, desired, error)
        except Exception as exc:  # noqa: BLE001
            order_failed.append({key: str(exc)})
            continue
        if outcome.get("ok"):
            order_restored.append(key)
        else:
            order_failed.append({key: outcome})
    restored = capture_graph(tree, error)
    nodes_equal = {row["name"]: row for row in restored["nodes"]} == {
        row["name"]: row for row in snapshot["nodes"]}
    links_equal = sorted((link_key(row), row.get("is_muted", False)) for row in restored["links"]) == sorted(
        (link_key(row), row.get("is_muted", False)) for row in snapshot["links"])
    if order_failed or not nodes_equal or not links_equal:
        raise error("rollback_failed", "the original graph could not be fully verified after restoration",
                    details={"rolled_back": False, "order_failed": order_failed,
                             "nodes_restored": nodes_equal, "links_restored": links_equal})
    return {"order_restored": order_restored, "order_failed": [], "verified": True}


# --------------------------------------------------------------------------- #
# layout payload helpers (reuse the add-on's own read-only serialisers)
# --------------------------------------------------------------------------- #
def _jsonable(api, value):
    converter = getattr(api, "_jsonable", None)
    if callable(converter):
        try:
            return converter(value)
        except Exception:  # noqa: BLE001
            pass
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if hasattr(value, "to_tuple"):
        try:
            return [float(item) for item in value.to_tuple()]
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (list, tuple)):
        return [_jsonable(api, item) for item in value]
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return {"name": name, "rna_type": getattr(getattr(value, "bl_rna", None), "identifier", None)}
    return None


def _socket_default(api, socket):
    if not hasattr(socket, "default_value"):
        return None
    if getattr(socket, "type", None) in ("GEOMETRY", "SHADER", "VIRTUAL"):
        return None
    try:
        return _jsonable(api, socket.default_value)
    except Exception:  # noqa: BLE001 - a socket we cannot read stays unread
        return None


def _socket_payload(api, sockets, limit: int = 64) -> list:
    payload = []
    for index, socket in enumerate(sockets):
        if index >= limit:
            break
        entry = {
            "index": index,
            "name": socket.name,
            "identifier": socket.identifier,
            "type": getattr(socket, "type", None),
            "is_linked": bool(getattr(socket, "is_linked", False)),
            "is_multi_input": bool(getattr(socket, "is_multi_input", False)),
            "link_count": len(getattr(socket, "links", []) or []),
        }
        default = _socket_default(api, socket)
        if default is not None:
            entry["default_value"] = default
        payload.append(entry)
    return payload


def _world(node, error):
    result = [float(value) for value in node.location]
    parent = getattr(node, "parent", None)
    seen = {node.as_pointer()}
    while parent is not None:
        if parent.as_pointer() in seen:
            raise error("parent_cycle", "the existing parent hierarchy of %r contains a cycle" % node.name)
        seen.add(parent.as_pointer())
        result = [a + float(b) for a, b in zip(result, parent.location)]
        parent = getattr(parent, "parent", None)
    return result


def _node_payload(api, node, error) -> dict:
    is_reroute = node.bl_idname == REROUTE_BL_IDNAME
    width = float(getattr(node, "width", 0.0) or 0.0)
    dimensions = [float(value) for value in (getattr(node, "dimensions", None) or [0.0, 0.0])]
    payload = {
        "name": node.name,
        "bl_idname": node.bl_idname,
        "bl_label": getattr(node, "bl_label", ""),
        "label": getattr(node, "label", ""),
        "mute": bool(getattr(node, "mute", False)),
        "location": [round(float(value), 3) for value in node.location],
        "location_absolute": [round(value, 3) for value in _world(node, error)],
        "parent": node.parent.name if getattr(node, "parent", None) else None,
        "width": round(width, 3),
        "dimensions": [round(value, 3) for value in dimensions],
        "layout_metrics_reliable": bool(width > 0.0 and (dimensions[0] > 0.0 or dimensions[1] > 0.0)),
        "is_frame": node.bl_idname == FRAME_BL_IDNAME,
        "is_reroute": is_reroute,
        "inputs": _socket_payload(api, node.inputs),
        "outputs": _socket_payload(api, node.outputs),
        "internal_links": len(getattr(node, "internal_links", []) or []),
    }
    if is_reroute:
        payload["reroute_sockets"] = {"inputs": len(node.inputs), "outputs": len(node.outputs)}
        payload["reroute_type"] = getattr(node, "type", None)
    if node.bl_idname in (GROUP_INPUT_BL_IDNAME, GROUP_OUTPUT_BL_IDNAME):
        payload["role"] = "group_interface"
    return payload


def _frame_payload(node, children, error) -> dict:
    return {
        "name": node.name,
        "label": getattr(node, "label", ""),
        "location": [round(float(value), 3) for value in node.location],
        "location_absolute": [round(value, 3) for value in _world(node, error)],
        "width": round(float(getattr(node, "width", 0.0) or 0.0), 3),
        "height": round(float(getattr(node, "height", 0.0) or 0.0), 3),
        "dimensions": [round(float(value), 3) for value in (getattr(node, "dimensions", None) or [0.0, 0.0])],
        "parent": node.parent.name if getattr(node, "parent", None) else None,
        "children": sorted(children),
        "shrink": bool(getattr(node, "shrink", False)),
    }


def _interface_payload(api, tree):
    """Reuse the add-on's interface serialiser when it is available."""
    builder = getattr(api, "_interface_payload", None)
    if callable(builder):
        try:
            payload = builder(tree)
        except Exception:  # noqa: BLE001 - fall back to a minimal report
            payload = None
        if isinstance(payload, dict):
            return payload
    interface = getattr(tree, "interface", None)
    items = getattr(interface, "items_tree", None)
    if items is None:
        return {"items": [], "inputs": [], "outputs": [], "panels": []}
    rows = []
    for index, item in enumerate(items):
        if index >= MAX_REPORT_INTERFACE:
            break
        rows.append({
            "name": item.name,
            "identifier": getattr(item, "identifier", None),
            "item_type": getattr(item, "item_type", None),
            "in_out": getattr(item, "in_out", None),
            "socket_type": getattr(item, "socket_type", None),
        })
    return {
        "items": rows,
        "inputs": [row for row in rows if row["in_out"] == "INPUT"],
        "outputs": [row for row in rows if row["in_out"] == "OUTPUT"],
        "panels": [row for row in rows if row["item_type"] == "PANEL"],
    }


def _modifier_users(api, tree) -> list:
    builder = getattr(api, "_group_modifier_users", None)
    if callable(builder):
        try:
            users = builder(tree)
        except Exception:  # noqa: BLE001
            users = None
        if isinstance(users, list):
            return users
    users = []
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []):
            group = getattr(mod, "node_group", None)
            if getattr(mod, "type", None) == "NODES" and group is not None and group.name == tree.name:
                users.append({"object": obj.name, "modifier": mod.name})
    return users


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _unique_group(group, error, write: bool = False):
    if not isinstance(group, str) or not group.strip() or "\x00" in group or len(group.encode("utf-8")) > 255:
        raise error("invalid_name", "group must be a non-empty exact node group name (max 255 UTF-8 bytes)")
    matches = [tree for tree in bpy.data.node_groups if tree.name == group]
    if len(matches) != 1:
        raise error(
            "ambiguous_name" if matches else "node_group_not_found",
            "expected exactly one node group named %r, found %d" % (group, len(matches)),
            details={"group": group, "matches": [tree.name for tree in matches]},
        )
    tree = matches[0]
    kind = _tree_type(tree)
    if kind != GEOMETRY_TREE_IDNAME:
        raise error(
            "unsupported_tree",
            "this command only handles %s, got %s" % (GEOMETRY_TREE_IDNAME, kind),
            details={"group": tree.name, "tree_type": kind},
        )
    if write and (getattr(tree, "library", None) or getattr(tree, "override_library", None)):
        raise error(
            "read_only_tree",
            "linked/override node trees cannot be edited: %r" % tree.name,
            details={"group": tree.name},
        )
    return tree


def _unique_object(name, error):
    if not isinstance(name, str) or not name.strip() or "\x00" in name or len(name.encode("utf-8")) > 255:
        raise error("invalid_name", "object must be a non-empty exact object name")
    matches = [obj for obj in bpy.data.objects if obj.name == name]
    if len(matches) != 1:
        raise error(
            "ambiguous_name" if matches else "object_not_found",
            "expected exactly one object named %r, found %d" % (name, len(matches)),
            details={"object": name},
        )
    return matches[0]


def _modifier_group(obj, modifier, error):
    mods = [mod for mod in getattr(obj, "modifiers", []) if getattr(mod, "type", None) == "NODES"]
    if modifier:
        mod = next((candidate for candidate in mods if candidate.name == modifier), None)
        if mod is None:
            raise error(
                "modifier_not_found",
                "object %r has no Geometry Nodes modifier named %r" % (obj.name, modifier),
                details={"object": obj.name, "geometry_nodes_modifiers": [m.name for m in mods]},
            )
    else:
        if not mods:
            raise error("modifier_not_found", "object %r has no Geometry Nodes modifier" % obj.name)
        if len(mods) > 1:
            raise error(
                "ambiguous_modifier",
                "object %r has %d Geometry Nodes modifiers; pass modifier=" % (obj.name, len(mods)),
                details={"object": obj.name, "geometry_nodes_modifiers": [m.name for m in mods]},
            )
        mod = mods[0]
    group = getattr(mod, "node_group", None)
    if group is None:
        raise error("no_node_group", "modifier %r on %r has no node group" % (mod.name, obj.name))
    return mod, group


def _resolve_target(group, object, modifier, error, write: bool = False):
    """``(tree, object_name, modifier_name)`` honouring the group/modifier pact."""
    tree = _unique_group(group, error, write=write)
    if object is None:
        if modifier is not None:
            raise error(
                "invalid_argument",
                "modifier was given without object; pass object= as well or omit modifier",
            )
        return tree, None, None
    obj = _unique_object(object, error)
    mod, mod_group = _modifier_group(obj, modifier, error)
    if mod_group.name != tree.name:
        raise error(
            "group_modifier_mismatch",
            "object %r modifier %r uses node group %r, not %r" % (obj.name, mod.name, mod_group.name, tree.name),
            details={
                "group": tree.name,
                "object": obj.name,
                "modifier": mod.name,
                "modifier_group": mod_group.name,
            },
        )
    return tree, obj.name, mod.name


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _graph_report(graph, tree_name) -> dict:
    """Bounded before/after picture used in every mutating response."""
    signature = semantic_signature(graph)
    links = graph["links"]
    return {
        "group": tree_name,
        "node_count": len(graph["nodes"]),
        "link_count": len(links),
        "reroute_count": len(reroute_set(graph)),
        "link_list_complete": len(links) <= MAX_REPORT_LINK_ROWS,
        "links": [
            {
                "from_node": row["from_node"],
                "from_socket": row["from_socket"],
                "to_node": row["to_node"],
                "to_socket": row["to_socket"],
                "multi_input_sort_id": row["multi_input_sort_id"],
                "multi_input": bool(row.get("to_multi_input")),
                "is_muted": bool(row.get("is_muted", False)),
            }
            for row in links[:MAX_REPORT_LINK_ROWS]
        ],
        "multi_input_orders": signature["multi_input_orders"],
        "signature": signature,
    }


def _flush(api, tree) -> dict:
    """Tag the group and every object that consumes it."""
    flush = getattr(api, "flush_group_users", None)
    if callable(flush):
        try:
            return {"tagged_objects": int(flush(tree)), "method": "flush_group_users"}
        except Exception as exc:  # noqa: BLE001 - tagging is best effort and reported
            return {"tagged_objects": None, "method": "flush_group_users",
                    "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        tree.update_tag()
        return {"tagged_objects": None, "method": "tree.update_tag"}
    except Exception as exc:  # noqa: BLE001
        return {"tagged_objects": None, "method": "none", "error": "%s: %s" % (type(exc).__name__, exc)}


def _preflight_multi_input(graph, sockets, error) -> None:
    """Refuse a write when this build cannot keep a multi-input order."""
    groups = multi_input_groups(graph)
    unsupported = []
    for key in sockets:
        rows = groups.get(key, [])
        if len(rows) <= 1:
            continue
        if not all(is_int(row.get("multi_input_sort_id")) for row in rows):
            unsupported.append(key)
    if unsupported:
        raise error(
            "multi_input_order_api_unavailable",
            "this Blender build exposes no usable multi-input link order for socket(s) %s "
            "(checked %s), so the existing order cannot be preserved or verified; refusing to rewire them"
            % (", ".join(sorted(unsupported)), ", ".join(MULTI_INPUT_ID_ATTRS)),
            details={"sockets": sorted(unsupported), "checked_attributes": list(MULTI_INPUT_ID_ATTRS)},
        )


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register_commands(api):
    """Register the five bounded Geometry Nodes canvas commands."""
    error = api.CommandError

    def finalize(tree, before_graph, after_graph, action, require_semantic=True):
        before_signature = semantic_signature(before_graph)
        after_signature = semantic_signature(after_graph)
        validation = compare_signatures(before_signature, after_signature)
        if require_semantic:
            equivalent = validation["equivalent"]
            reason = "%s changed the resolved data flow of %r; the edit must be rolled back" % (action, tree.name)
        else:
            # A pure move cannot change wiring, so only the topology is asserted;
            # this keeps a graph that was ALREADY degenerate (for example with a
            # pre-existing reroute cycle) from blocking an unrelated move.
            links_equal = ([link_key(row) for row in before_graph["links"]]
                           == [link_key(row) for row in after_graph["links"]])
            equivalent = bool(links_equal
                              and before_signature["reroute_cycle"] == after_signature["reroute_cycle"])
            validation["topology_unchanged"] = links_equal
            reason = "%s changed the link topology of %r; the edit must be rolled back" % (action, tree.name)
        tagged = _flush(api, tree)
        if not equivalent:
            raise error(
                "verification_failed",
                reason,
                details={
                    "rolled_back": True,
                    "group": tree.name,
                    "validation": validation,
                    "before": _graph_report(before_graph, tree.name),
                    "after": _graph_report(after_graph, tree.name),
                },
            )
        return validation, tagged

    def guarded(action, tree, snapshot, before_graph, work, require_semantic=True):
        """Run ``work``, roll the whole edit back on any failure."""
        try:
            work()
            after_graph = _graph_view(tree)
            validation, tagged = finalize(tree, before_graph, after_graph, action, require_semantic)
        except Exception as exc:  # noqa: BLE001 - every failure is reported as a code
            details = {"group": tree.name, "action": action}
            if isinstance(exc, error):
                details.update(dict(exc.details or {}))
            try:
                details.setdefault("validation", compare_signatures(
                    semantic_signature(before_graph), semantic_signature(_graph_view(tree))))
            except Exception:  # noqa: BLE001 - the failure itself is the important part
                pass
            try:
                details["rollback"] = restore_graph(tree, snapshot, error)
                _flush(api, tree)
                details["rolled_back"] = True
            except Exception as rollback_exc:  # noqa: BLE001
                details["rolled_back"] = False
                details["rollback_error"] = str(rollback_exc)
            if isinstance(exc, error):
                raise error(exc.code, exc.message, details=details) from exc
            raise error(
                "%s_failed" % action,
                "%s: %s" % (type(exc).__name__, exc),
                details=details,
            ) from exc
        return after_graph, validation, tagged

    @api.command(
        "geometry_nodes_layout",
        mutating=False,
        summary="read-only layout/sockets/links/frames/reroutes of one exact GeometryNodeTree",
    )
    def layout(group, object=None, modifier=None):
        tree, object_name, modifier_name = _resolve_target(group, object, modifier, error)
        graph = _graph_view(tree)
        nodes = list(tree.nodes)
        links = list(tree.links)
        reroutes = reroute_set(graph)
        children: dict = {}
        for node in nodes:
            parent = getattr(node, "parent", None)
            if parent is not None:
                children.setdefault(parent.name, []).append(node.name)
        multi_orders = {}
        for key, rows in sorted(multi_input_groups(graph).items()):
            multi_orders[key] = [_source_label(graph, reroutes, row) for row in ordered_rows(rows)]
        return {
            "group": tree.name,
            "tree_type": _tree_type(tree),
            "is_geometry_tree": _tree_type(tree) == GEOMETRY_TREE_IDNAME,
            "library": getattr(getattr(tree, "library", None), "name", None),
            "override_library": bool(getattr(tree, "override_library", None)),
            "object": object_name,
            "modifier": modifier_name,
            "users": int(getattr(tree, "users", 0) or 0),
            "modifier_users": _modifier_users(api, tree),
            "counts": {
                "nodes": len(nodes),
                "links": len(links),
                "frames": len([node for node in nodes if node.bl_idname == FRAME_BL_IDNAME]),
                "reroutes": len([node for node in nodes if node.bl_idname == REROUTE_BL_IDNAME]),
                "multi_input_sockets": len(multi_orders),
            },
            "nodes": [_node_payload(api, node, error) for node in nodes[:MAX_REPORT_NODES]],
            "nodes_truncated": max(0, len(nodes) - MAX_REPORT_NODES),
            "links": [
                {
                    "from_node": link.from_node.name,
                    "from_socket": link.from_socket.identifier,
                    "from_socket_name": link.from_socket.name,
                    "to_node": link.to_node.name,
                    "to_socket": link.to_socket.identifier,
                    "to_socket_name": link.to_socket.name,
                    "is_valid": bool(getattr(link, "is_valid", True)),
                    "is_muted": bool(getattr(link, "is_muted", False)),
                    "multi_input": _is_multi_input(link.to_socket),
                    "multi_input_sort_id": _read_sort_id(link),
                    "multi_input_order": _multi_input_order_of(graph, link),
                    "is_reroute_link": link.from_node.bl_idname == REROUTE_BL_IDNAME
                    or link.to_node.bl_idname == REROUTE_BL_IDNAME,
                }
                for link in links[:MAX_REPORT_LINKS]
            ],
            "links_truncated": max(0, len(links) - MAX_REPORT_LINKS),
            "frames": [
                _frame_payload(node, children.get(node.name, []), error)
                for node in nodes
                if node.bl_idname == FRAME_BL_IDNAME
            ],
            "reroutes": [
                {
                    "name": node.name,
                    "location": [round(float(value), 3) for value in node.location],
                    "location_absolute": [round(value, 3) for value in _world(node, error)],
                    "parent": node.parent.name if getattr(node, "parent", None) else None,
                    "type": getattr(node, "type", None),
                    "inputs": len(node.inputs),
                    "outputs": len(node.outputs),
                    "incoming": len([row for row in graph["links"] if row["to_node"] == node.name]),
                    "outgoing": len(reroute_output_rows(graph, node.name)),
                }
                for node in nodes
                if node.bl_idname == REROUTE_BL_IDNAME
            ],
            "interface": _interface_payload(api, tree),
            "multi_input_sockets": [
                {"socket": key, "order": order} for key, order in multi_orders.items()
            ],
            "signature": semantic_signature(graph),
            "notes": [
                "location is canvas-local; location_absolute adds every parent Frame offset",
                "width/dimensions are only meaningful once a node editor has drawn this tree; "
                "layout_metrics_reliable is false while they are still zero",
                "multi_input_sort_id is null when this Blender build does not expose it; multi_input_order "
                "is then derived from the captured link order",
                "this command never writes and never rearranges anything",
            ],
        }

    def _multi_input_order_of(graph, link):
        if not _is_multi_input(link.to_socket):
            return None
        rows = multi_input_groups(graph).get(socket_key(link.to_node.name, link.to_socket.identifier), [])
        for index, row in enumerate(ordered_rows(rows)):
            if link_key(row) == _link_key_of(link):
                return index
        return None

    def move(tree, plan, absolute, action):
        snapshot = capture_graph(tree, error)
        before_graph = dict(snapshot)
        moved = []

        def work():
            for item in plan:
                node = tree.nodes.get(item["node"])
                if node is None:
                    raise error("node_not_found", "node %r disappeared during the edit" % item["node"])
                before_local = [float(value) for value in node.location]
                before_absolute = _world(node, error)
                if absolute:
                    base = _world(node.parent, error) if getattr(node, "parent", None) else [0.0, 0.0]
                    node.location = [item["x"] - base[0], item["y"] - base[1]]
                else:
                    node.location = [item["x"], item["y"]]
                moved.append({
                    "node": item["node"],
                    "bl_idname": item["bl_idname"],
                    "parent": node.parent.name if getattr(node, "parent", None) else None,
                    "before": [round(value, 3) for value in before_local],
                    "after": [round(float(value), 3) for value in node.location],
                    "before_absolute": [round(value, 3) for value in before_absolute],
                    "after_absolute": [round(value, 3) for value in _world(node, error)],
                    "requested": [item["x"], item["y"]],
                })
            return None

        after_graph, validation, tagged = guarded(
            action, tree, snapshot, before_graph, work, require_semantic=False
        )
        applied = all(
            item["after"] == item["requested"]
            if not absolute
            else item["after_absolute"] == item["requested"]
            for item in moved
        )
        links_unchanged = (
            [list(link_key(row)) for row in before_graph["links"]]
            == [list(link_key(row)) for row in after_graph["links"]]
        )
        return {
            "group": tree.name,
            "absolute": bool(absolute),
            "moved": moved,
            "moved_count": len(moved),
            "unchanged": all(item["before"] == item["after"] for item in moved),
            "links_unchanged": links_unchanged,
            "before": _graph_report(before_graph, tree.name),
            "after": _graph_report(after_graph, tree.name),
            "validation": dict(
                validation,
                node_locations_applied=applied,
                links_unchanged=links_unchanged,
            ),
            "tagged": tagged,
            "rolled_back": False,
        }

    @api.command(
        "geometry_nodes_move_nodes",
        summary="move explicitly named nodes (local or absolute canvas coordinates)",
    )
    def move_nodes(group, positions, absolute=False):
        if not isinstance(absolute, bool):
            raise error("invalid_argument", "absolute must be boolean")
        tree, _object, _modifier = _resolve_target(group, None, None, error, write=True)
        graph = _graph_view(tree)
        plan, problems = plan_move_items(graph, positions, reroutes_only=False)
        if problems:
            raise error(
                "invalid_positions",
                plan_problem_message("move_nodes", problems),
                details={"problems": problems},
            )
        return move(tree, plan, absolute, "move_nodes")

    @api.command(
        "geometry_nodes_reroute_insert",
        summary="insert native NodeReroute nodes into existing links (single, one-to-many, multi-input)",
    )
    def reroute_insert(group, links, points=None, shared_branch=False):
        tree, _object, _modifier = _resolve_target(group, None, None, error, write=True)
        graph = _graph_view(tree)
        plan, problems = plan_reroute_insert(graph, links, points, shared_branch)
        if problems:
            raise error(
                "invalid_links",
                plan_problem_message("reroute_insert", problems),
                details={"problems": problems},
            )
        _preflight_multi_input(graph, plan["affected_sockets"], error)
        snapshot = capture_graph(tree, error)
        before_graph = dict(snapshot)
        created = []
        order_reports = []

        def work():
            for item in plan["reroutes"]:
                node = tree.nodes.new(REROUTE_BL_IDNAME)
                created.append(node)
                if len(node.inputs) != 1 or len(node.outputs) < 1:
                    raise error(
                        "unexpected_reroute_sockets",
                        "NodeReroute in this build has %d input(s) and %d output(s); this command expects one "
                        "input and at least one output" % (len(node.inputs), len(node.outputs)),
                        details={"inputs": len(node.inputs), "outputs": len(node.outputs)},
                    )
                node.name = item["name"]
                if node.name != item["name"]:
                    raise error(
                        "invalid_name",
                        "Blender renamed the new reroute to %r instead of %r" % (node.name, item["name"]),
                    )
                node.location = [item["location"][0], item["location"][1]]
            for item, node in zip(plan["reroutes"], created):
                mutes = {tuple(target["replace"]): bool(getattr(
                    _find_link(tree, target["replace"], error), "is_muted", False))
                    for target in item["targets"]}
                for target in item["targets"]:
                    tree.links.remove(_find_link(tree, target["replace"], error))
                _new_link(tree, item["source"]["node"], item["source"]["socket"],
                          node.name, node.inputs[0].identifier, error)
                for target in item["targets"]:
                    _new_link(tree, node.name, node.outputs[0].identifier,
                              target["node"], target["socket"], error,
                              muted=mutes[tuple(target["replace"])])
            for entry in plan["order"]:
                desired = _order_entries_to_keys(entry["entries"], created, error)
                socket = _socket_or_error(tree, entry["node"], entry["socket_name"], "INPUT", error)
                outcome = _apply_link_order(tree, socket, desired, error)
                order_reports.append({"socket": entry["socket"], **outcome})
                if not outcome.get("ok"):
                    raise error(
                        "verification_failed",
                        "could not restore the multi-input order of %s after inserting reroutes" % entry["socket"],
                        details={"socket": entry["socket"], "order": outcome},
                    )
            return None

        after_graph, validation, tagged = guarded(
            "reroute_insert", tree, snapshot, before_graph, work
        )
        socket_counts = {"inputs": len(created[0].inputs), "outputs": len(created[0].outputs)} if created else None
        return {
            "group": tree.name,
            "shared_branch": bool(shared_branch),
            "created": [
                {
                    "node": node.name,
                    "location": [round(float(value), 3) for value in node.location],
                    "source": item["source"],
                    "targets": [
                        {
                            "node": target["node"],
                            "socket": target["socket"],
                            "multi_input_sort_id": target["multi_input_sort_id"],
                            "multi_input": target["multi_input"],
                        }
                        for target in item["targets"]
                    ],
                    "spec_indexes": item["spec_indexes"],
                    "replaced": [target["replace"] for target in item["targets"]],
                }
                for item, node in zip(plan["reroutes"], created)
            ],
            "created_count": len(created),
            "positions_source": plan["positions_source"],
            "reroute_sockets": socket_counts,
            "multi_input_order": order_reports,
            "multi_input_order_method": sorted(
                {report.get("method") for report in order_reports if report.get("method")}
            ),
            "before": _graph_report(before_graph, tree.name),
            "after": _graph_report(after_graph, tree.name),
            "validation": validation,
            "tagged": tagged,
            "warnings": [
                "Reroute nodes are canvas anchors for the very same wires; Blender still draws the same curves "
                "and they do not make wiring orthogonal.",
            ],
            "rolled_back": False,
        }

    @api.command(
        "geometry_nodes_reroute_move",
        summary="move explicitly named reroute nodes only",
    )
    def reroute_move(group, positions, absolute=False):
        if not isinstance(absolute, bool):
            raise error("invalid_argument", "absolute must be boolean")
        tree, _object, _modifier = _resolve_target(group, None, None, error, write=True)
        graph = _graph_view(tree)
        plan, problems = plan_move_items(graph, positions, reroutes_only=True)
        if problems:
            raise error(
                "invalid_positions",
                plan_problem_message("reroute_move", problems),
                details={"problems": problems},
            )
        return move(tree, plan, absolute, "reroute_move")

    @api.command(
        "geometry_nodes_reroute_delete",
        summary="delete named reroutes, reconnecting each source to every downstream target",
    )
    def reroute_delete(group, nodes):
        tree, _object, _modifier = _resolve_target(group, None, None, error, write=True)
        graph = _graph_view(tree)
        plan, problems = plan_reroute_delete(graph, nodes)
        if problems:
            raise error(
                "invalid_nodes",
                plan_problem_message("reroute_delete", problems),
                details={"problems": problems},
            )
        _preflight_multi_input(graph, plan["affected_sockets"], error)
        snapshot = capture_graph(tree, error)
        before_graph = dict(snapshot)
        order_reports = []
        removed = []
        deleting = set(plan["nodes"])

        def work():
            for entry in plan["reconnects"]:
                _new_link(tree, entry["source"]["node"], entry["source"]["socket"],
                          entry["target"]["node"], entry["target"]["socket"], error,
                          muted=entry["is_muted"])
            victims = [
                link for link in list(tree.links)
                if link.from_node.name in deleting or link.to_node.name in deleting
            ]
            removed.extend(_link_key_of(link) for link in victims)
            for link in victims:
                tree.links.remove(link)
            for name in plan["nodes"]:
                node = tree.nodes.get(name)
                if node is None:
                    raise error("node_not_found", "reroute %r disappeared during the edit" % name)
                if node.bl_idname != REROUTE_BL_IDNAME:
                    raise error("wrong_node_type", "node %r is no longer a NodeReroute" % name)
                tree.nodes.remove(node)
            for entry in plan["order"]:
                desired = [item["key"] for item in entry["entries"]]
                socket = _socket_or_error(tree, entry["node"], entry["socket_name"], "INPUT", error)
                outcome = _apply_link_order(tree, socket, desired, error)
                order_reports.append({"socket": entry["socket"], **outcome})
                if not outcome.get("ok"):
                    raise error(
                        "verification_failed",
                        "could not restore the multi-input order of %s after deleting reroutes" % entry["socket"],
                        details={"socket": entry["socket"], "order": outcome},
                    )
            return None

        after_graph, validation, tagged = guarded(
            "reroute_delete", tree, snapshot, before_graph, work
        )
        return {
            "group": tree.name,
            "deleted": plan["nodes"],
            "deleted_count": len(plan["nodes"]),
            "reconnected": plan["reconnects"],
            "dead_ends": plan["dead_ends"],
            "links_removed": len(removed),
            "multi_input_order": order_reports,
            "multi_input_order_method": sorted(
                {report.get("method") for report in order_reports if report.get("method")}
            ),
            "before": _graph_report(before_graph, tree.name),
            "after": _graph_report(after_graph, tree.name),
            "validation": validation,
            "tagged": tagged,
            "warnings": (
                ["dead-end reroute(s) removed with no downstream target: %s" % ", ".join(plan["dead_ends"])]
                if plan["dead_ends"] else []
            ),
            "rolled_back": False,
        }
