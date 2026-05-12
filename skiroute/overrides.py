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
        ]
    }

Any entry with `"_example": true` is skipped — handy for templates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from skiroute.geometry import haversine
from skiroute.graph import Edge

if TYPE_CHECKING:
    from skiroute.graph import Graph


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
        from_matches = graph.find_nodes_by_name(edge_def["from_name"])
        to_matches = graph.find_nodes_by_name(edge_def["to_name"])
        if not from_matches:
            if verbose:
                print(f"    WARNING: override from_name '{edge_def['from_name']}' not found")
            continue
        if not to_matches:
            if verbose:
                print(f"    WARNING: override to_name '{edge_def['to_name']}' not found")
            continue
        a, b = from_matches[0], to_matches[0]
        dist = haversine(a.lon, a.lat, b.lon, b.lat)
        cost = edge_def.get("cost_override", dist / 2 + 30)
        edge = Edge(
            from_id=a.id, to_id=b.id, type=edge_def.get("type", "skate"),
            name=edge_def.get("name", f"Override: {a.name} → {b.name}"),
            difficulty=edge_def.get("difficulty", ""),
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
        from_matches = graph.find_nodes_by_name(block_def["from_name"])
        to_matches = graph.find_nodes_by_name(block_def["to_name"])
        if not from_matches or not to_matches:
            continue
        a_id, b_id = from_matches[0].id, to_matches[0].id
        block_type = block_def.get("type")
        to_remove = [
            idx for idx in graph.adjacency.get(a_id, [])
            if graph.edges[idx].to_id == b_id
            and (not block_type or graph.edges[idx].type == block_type)
        ]
        for idx in to_remove:
            graph.adjacency[a_id].remove(idx)
            blocked += 1

    # Cost adjustments
    for adj_def in overrides.get("cost_adjustments", []):
        if adj_def.get("_example"):
            continue
        from_matches = graph.find_nodes_by_name(adj_def["from_name"])
        to_matches = graph.find_nodes_by_name(adj_def["to_name"])
        if not from_matches or not to_matches:
            continue
        a_id, b_id = from_matches[0].id, to_matches[0].id
        adj_type = adj_def.get("type")
        mult = adj_def.get("cost_multiplier", 1.0)
        for idx in graph.adjacency.get(a_id, []):
            e = graph.edges[idx]
            if e.to_id == b_id and (not adj_type or e.type == adj_type):
                e.cost_base *= mult
                adjusted += 1

    if verbose:
        print(f"  Applied overrides: +{added_nodes} nodes, +{added_edges} edges, "
              f"-{blocked} blocked, ~{adjusted} adjusted")
    graph._invalidate_cache()
