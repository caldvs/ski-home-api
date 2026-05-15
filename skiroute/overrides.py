"""Graph overrides — manual patches loaded after the automatic build.

Real-world OpenSkiData has gaps, mis-named runs, and occasional broken
geometry. The overrides system lets you fix these without modifying
the builder logic. Overrides are loaded from a JSON file with this shape:

    {
        "add_nodes": [
            {"name": "Petit Col", "lon": 6.94, "lat": 45.46, "elev": 2300,
             "destinations": ["Tignes Le Lac"]}
        ],
        "add_edges": [
            {"from_name": "Solaise top", "to_name": "Bellevarde Express top",
             "type": "skate", "cost_override": 180}
        ],
        "block_edges": [
            {"from_name": "Tovière top", "to_name": "Tovière bottom",
             "type": "run"}
        ],
        "cost_adjustments": [
            {"from_name": "Cugnai top", "to_name": "Cugnai bottom",
             "cost_multiplier": 3.0}
        ],
        "remove_named_features": [
            {"name": "Golf",
             "_comment": "Orphan piste with no real-world exit"}
        ],
        "rename_features": [
            {"old_name": "Run 162043", "new_name": "Couloir des Tufs",
             "_comment": "Give an unnamed OSM segment its real name"}
        ],
        "set_difficulty": [
            {"name": "Descente", "difficulty": "intermediate",
             "_comment": "OSM tagged this as advanced; in practice it's a red"}
        ]
    }

Any entry with `"_example": true` is skipped — handy for templates.

The three name-keyed operations (``remove_named_features``,
``rename_features``, ``set_difficulty``) are what the piste editor UI
emits — they let you address an entire named feature at once instead
of every individual edge that makes it up.

Endpoint disambiguation
-----------------------
``add_edges``, ``block_edges``, ``cost_adjustments`` and ``remove_edges``
all accept EITHER ``from_name``/``to_name`` (substring match) OR
``from_id``/``to_id`` (exact). When both endpoints share a name in the
graph (e.g. multiple "Verte bottom" nodes 300 m apart) the ID form is
required — the name form picks the first match and silently lands on
the wrong node.

Directionality
--------------
``add_edges`` creates a SINGLE directed edge. Skiroute models lifts,
runs and skates as one-way: a piste runs from top to bottom, a chairlift
runs bottom to top, a skate connector can be one-way uphill or down.
For a *bidirectional* connector (a flat 'piste' link a skier can ski
in either direction), add TWO entries with ``from``/``to`` swapped.

Application order
-----------------
Operations are applied in this fixed order inside ``apply_overrides``:

  1. add_nodes
  2. add_edges
  3. block_edges
  4. cost_adjustments
  5. remove_named_features
  6. rename_features
  7. remove_edges
  8. set_difficulty

This matters when ops in the same file would conflict — e.g. if you
``add_edges`` referencing a node that another op in the same file
removes, the added edge survives but points at a node that no longer
exists. Bias toward small, focused override files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from skiroute.geometry import haversine
from skiroute.graph import Edge

if TYPE_CHECKING:
    from skiroute.graph import Graph


def _resolve_endpoint(
    graph: "Graph", spec: dict, prefix: str, verbose: bool = True,
) -> "Node | None":
    """Look up the from/to node for an override spec.

    Each spec may use ``{prefix}_id`` (preferred — unambiguous when names
    collide) OR ``{prefix}_name`` (substring match). Returns the first
    matching Node or None.

    If the name form matches >1 node, prints a warning naming the matches
    and which one was chosen — names are substring-matched and the first
    match is iteration-order dependent. The user should switch to the
    ``_id`` form to disambiguate.
    """
    nid = spec.get(f"{prefix}_id")
    if nid is not None:
        return graph.nodes.get(nid)
    name = spec.get(f"{prefix}_name")
    if name:
        matches = graph.find_nodes_by_name(name)
        if not matches:
            return None
        if len(matches) > 1 and verbose:
            sample = ", ".join(f"id={m.id}({m.elev:.0f}m)" for m in matches[:5])
            print(f"    WARNING: {prefix}_name='{name}' matched {len(matches)} nodes "
                  f"({sample}); picking id={matches[0].id}. "
                  f"Use {prefix}_id=N to disambiguate.")
        return matches[0]
    return None


def _endpoint_label(spec: dict, prefix: str) -> str:
    """Human-readable label of an override endpoint (for log messages)."""
    nid = spec.get(f"{prefix}_id")
    if nid is not None:
        return f"id={nid}"
    return f"name={spec.get(f'{prefix}_name')!r}"


def apply_overrides(graph: "Graph", overrides_path: str | Path, verbose: bool = True) -> None:
    """Apply node additions, edge additions, blocks, and cost tweaks."""
    path = Path(overrides_path)
    if not path.exists():
        if verbose:
            print(f"  No overrides file at {path}")
        return

    with path.open() as f:
        overrides = json.load(f)

    added_nodes = added_edges = blocked = adjusted = 0
    removed_edges = removed_nodes = renamed = retyped = 0
    removed_named_edges = 0
    next_id = max(graph.nodes) + 1 if graph.nodes else 0

    # Add nodes
    for node_def in overrides.get("add_nodes", []):
        if node_def.get("_example"):
            continue
        from skiroute.graph import Node
        node = Node(
            id=next_id, lon=node_def["lon"], lat=node_def["lat"],
            elev=node_def["elev"], name=node_def["name"],
            destinations=node_def.get(
                "destinations", node_def.get("villages", [])),
        )
        graph.nodes[next_id] = node
        next_id += 1
        added_nodes += 1

    # Add edges
    for edge_def in overrides.get("add_edges", []):
        if edge_def.get("_example"):
            continue
        a = _resolve_endpoint(graph, edge_def, "from", verbose=verbose)
        b = _resolve_endpoint(graph, edge_def, "to", verbose=verbose)
        if a is None:
            if verbose:
                print(f"    WARNING: add_edges from {_endpoint_label(edge_def, 'from')} not found")
            continue
        if b is None:
            if verbose:
                print(f"    WARNING: add_edges to {_endpoint_label(edge_def, 'to')} not found")
            continue
        dist = haversine(a.lon, a.lat, b.lon, b.lat)
        cost = edge_def.get("cost_override", dist / 2 + 30)
        edge = Edge(
            from_id=a.id, to_id=b.id, type=edge_def.get("type", "skate"),
            name=edge_def.get("name", f"Override: {a.name} → {b.name}"),
            difficulty=edge_def.get("difficulty", ""),
            lift_type=edge_def.get("lift_type", ""),
            elev_drop=edge_def.get("elev_drop", max(0.0, a.elev - b.elev)),
            length_m=dist, cost_base=cost,
        )
        edge.geometry = [(a.lon, a.lat), (b.lon, b.lat)]
        idx = len(graph.edges)
        graph.edges.append(edge)
        graph.adjacency[a.id].append(idx)
        added_edges += 1

    # Block edges
    for block_def in overrides.get("block_edges", []):
        if block_def.get("_example"):
            continue
        a = _resolve_endpoint(graph, block_def, "from", verbose=verbose)
        b = _resolve_endpoint(graph, block_def, "to", verbose=verbose)
        if a is None or b is None:
            continue
        block_type = block_def.get("type")
        to_remove = [
            idx for idx in graph.adjacency.get(a.id, [])
            if graph.edges[idx].to_id == b.id
            and (not block_type or graph.edges[idx].type == block_type)
        ]
        for idx in to_remove:
            graph.adjacency[a.id].remove(idx)
            blocked += 1

    # Cost adjustments
    for adj_def in overrides.get("cost_adjustments", []):
        if adj_def.get("_example"):
            continue
        a = _resolve_endpoint(graph, adj_def, "from", verbose=verbose)
        b = _resolve_endpoint(graph, adj_def, "to", verbose=verbose)
        if a is None or b is None:
            continue
        adj_type = adj_def.get("type")
        mult = adj_def.get("cost_multiplier", 1.0)
        for idx in graph.adjacency.get(a.id, []):
            e = graph.edges[idx]
            if e.to_id == b.id and (not adj_type or e.type == adj_type):
                e.cost_base *= mult
                adjusted += 1

    # Remove named features — drops every edge whose .name matches, then
    # any nodes that are no longer incident to any edge. Used to retire
    # orphaned/illegitimate pistes (e.g. truncated geometry that can't
    # plausibly route anywhere).
    for spec in overrides.get("remove_named_features", []):
        if spec.get("_example"):
            continue
        target_name = spec["name"]
        keep_edges: list[Edge] = []
        dropped_edge_idxs: set[int] = set()
        for idx, e in enumerate(graph.edges):
            if e.name == target_name:
                dropped_edge_idxs.add(idx)
                removed_edges += 1
            else:
                keep_edges.append(e)
        if not dropped_edge_idxs:
            if verbose:
                print(f"    WARNING: remove_named_features '{target_name}' matched no edges")
            continue
        # Rebuild the edge list AND the adjacency to keep indices consistent.
        graph.edges = keep_edges
        graph.adjacency.clear()
        for new_idx, e in enumerate(graph.edges):
            graph.adjacency.setdefault(e.from_id, []).append(new_idx)
        # Drop nodes that no longer have any incident edge (in either direction).
        incident: set[int] = set()
        for e in graph.edges:
            incident.add(e.from_id)
            incident.add(e.to_id)
        orphan_ids = [nid for nid in graph.nodes if nid not in incident]
        for nid in orphan_ids:
            del graph.nodes[nid]
            removed_nodes += 1
        graph._invalidate_cache()

    # Rename features — change .name across every edge with the old name.
    # Also rewrites node names that match exactly (e.g. "Golf top" → "Foo top"
    # when the feature is renamed Golf → Foo). The node-rename heuristic is:
    # if a node's name starts with the old feature name followed by a space
    # qualifier (" top", " bottom"), the prefix is swapped.
    for spec in overrides.get("rename_features", []):
        if spec.get("_example"):
            continue
        old, new = spec["old_name"], spec["new_name"]
        edge_hits = 0
        for e in graph.edges:
            if e.name == old:
                e.name = new
                edge_hits += 1
        if not edge_hits:
            if verbose:
                print(f"    WARNING: rename_features '{old}' matched no edges")
            continue
        renamed += edge_hits
        # Best-effort node rename
        for n in graph.nodes.values():
            if n.name == old:
                n.name = new
            elif n.name.startswith(old + " "):
                n.name = new + n.name[len(old):]

    # Remove specific edges — like remove_named_features but per-edge,
    # for surgically deleting a single broken/shard segment without
    # touching the rest of a named feature. Matches by (from_name, to_name,
    # optional type). Also drops any nodes that become unincident.
    edges_to_drop: set[int] = set()
    for spec in overrides.get("remove_edges", []):
        if spec.get("_example"):
            continue
        a = _resolve_endpoint(graph, spec, "from", verbose=verbose)
        b = _resolve_endpoint(graph, spec, "to", verbose=verbose)
        if a is None or b is None:
            if verbose:
                print(f"    WARNING: remove_edges {_endpoint_label(spec, 'from')} → "
                      f"{_endpoint_label(spec, 'to')} did not resolve")
            continue
        want_type = spec.get("type")
        for idx, e in enumerate(graph.edges):
            if e.from_id == a.id and e.to_id == b.id \
                    and (not want_type or e.type == want_type):
                edges_to_drop.add(idx)
    if edges_to_drop:
        kept_edges: list[Edge] = [
            e for i, e in enumerate(graph.edges) if i not in edges_to_drop
        ]
        removed_named_edges = len(graph.edges) - len(kept_edges)
        graph.edges = kept_edges
        graph.adjacency.clear()
        for new_idx, e in enumerate(graph.edges):
            graph.adjacency.setdefault(e.from_id, []).append(new_idx)
        incident: set[int] = set()
        for e in graph.edges:
            incident.add(e.from_id); incident.add(e.to_id)
        orphan_ids = [nid for nid in graph.nodes if nid not in incident]
        for nid in orphan_ids:
            del graph.nodes[nid]
            removed_nodes += 1
        graph._invalidate_cache()

    # Set difficulty — overwrite the difficulty field across every
    # run/connection edge with the given feature name. Useful when OSM
    # has it wrong or you want to unify variants ("Descente du Glacier"
    # really being an intermediate, etc).
    for spec in overrides.get("set_difficulty", []):
        if spec.get("_example"):
            continue
        target, new_diff = spec["name"], spec["difficulty"]
        hits = 0
        for e in graph.edges:
            if e.name == target and e.type in ("run", "connection"):
                e.difficulty = new_diff
                hits += 1
        if not hits and verbose:
            print(f"    WARNING: set_difficulty '{target}' matched no run/connection edges")
        retyped += hits

    if verbose:
        # Only print fields where something actually changed — a long
        # zero-stuffed line on every build was noisy.
        parts = []
        if added_nodes:        parts.append(f"+{added_nodes} nodes")
        if added_edges:        parts.append(f"+{added_edges} edges")
        if blocked:            parts.append(f"-{blocked} blocked")
        if adjusted:           parts.append(f"~{adjusted} adjusted")
        if removed_edges:      parts.append(f"-{removed_edges} feature-edges")
        if removed_named_edges: parts.append(f"-{removed_named_edges} segments")
        if removed_nodes:      parts.append(f"-{removed_nodes} nodes")
        if renamed:            parts.append(f"~{renamed} renamed")
        if retyped:            parts.append(f"~{retyped} retyped")
        if parts:
            print(f"  Applied overrides: {', '.join(parts)}")
        else:
            print("  Applied overrides: (no-op)")
    # Invalidate the reverse-adjacency cache since we may have added or
    # removed edges. Anyone who called graph.reverse_adjacency() before
    # the overrides applied gets a stale view otherwise.
    graph._invalidate_cache()
