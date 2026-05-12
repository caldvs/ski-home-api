"""Graph data structures: Node, Edge, and the Graph container.

The on-disk JSON format is **compatible with the existing ski-home
graph.json shape** so iOS / API clients keep working unchanged.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from skiroute.geometry import haversine


EdgeType = str  # "run" | "lift" | "lift_down" | "skate" | "connection"


@dataclass
class Node:
    id: int
    lon: float
    lat: float
    elev: float
    name: str = ""
    destinations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lon": round(self.lon, 6),
            "lat": round(self.lat, 6),
            "elev": round(self.elev, 1),
            "name": self.name,
            "villages": self.destinations,  # JSON key kept for compatibility
        }


@dataclass
class Edge:
    from_id: int
    to_id: int
    type: EdgeType
    name: str = ""
    cost_base: float = 0.0
    length_m: float = 0.0
    elev_drop: float = 0.0
    difficulty: str = ""
    lift_type: str = ""
    geometry: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "from": self.from_id,
            "to": self.to_id,
            "type": self.type,
            "name": self.name,
            "cost_base": round(self.cost_base, 1),
            "length_m": round(self.length_m, 1),
            "elev_drop": round(self.elev_drop, 1),
        }
        if self.difficulty:
            d["difficulty"] = self.difficulty
        if self.lift_type:
            d["lift_type"] = self.lift_type
        if self.geometry:
            d["geometry"] = [[round(c[0], 6), round(c[1], 6)] for c in self.geometry]
        return d


class Graph:
    """A routable ski-resort graph.

    Use `skiroute.build_graph(...)` to construct one from an OpenSkiData
    GPKG, or `Graph.load(path)` to read a previously saved graph.
    """

    def __init__(self):
        self.nodes: dict[int, Node] = {}
        self.edges: list[Edge] = []
        self.adjacency: dict[int, list[int]] = defaultdict(list)
        self.destinations: dict[str, dict] = {}  # name -> {lat, lon, elev}
        self._reverse_adjacency: dict[int, list[int]] | None = None  # lazy

    # --- introspection ---

    def __repr__(self) -> str:
        return f"Graph(nodes={len(self.nodes)}, edges={len(self.edges)}, destinations={len(self.destinations)})"

    def find_nearest_node(self, lon: float, lat: float) -> tuple[Node, float]:
        """Return (node, distance_m) for the closest graph node.

        Raises ValueError if the graph contains no nodes.
        """
        if not self.nodes:
            raise ValueError("Graph has no nodes")
        best, best_dist = None, float("inf")
        for node in self.nodes.values():
            d = haversine(lon, lat, node.lon, node.lat)
            if d < best_dist:
                best_dist = d
                best = node
        return best, best_dist

    def find_nodes_by_name(self, name: str) -> list[Node]:
        """Case-insensitive substring match against node names."""
        needle = name.lower()
        return [n for n in self.nodes.values() if n.name and needle in n.name.lower()]

    def nodes_for_destination(self, destination: str) -> set[int]:
        return {nid for nid, n in self.nodes.items() if destination in n.destinations}

    def adjacent_edges(self, node_id: int) -> Iterable[Edge]:
        for idx in self.adjacency.get(node_id, []):
            yield self.edges[idx]

    def reverse_adjacency(self) -> dict[int, list[int]]:
        """Lazily compute and cache reverse adjacency.

        For each node v, lists the edge indices of edges *arriving* at v.
        Invalidated automatically if edges are added through `_invalidate_cache`.
        """
        if self._reverse_adjacency is None:
            rev: dict[int, list[int]] = defaultdict(list)
            for idx, e in enumerate(self.edges):
                rev[e.to_id].append(idx)
            self._reverse_adjacency = rev
        return self._reverse_adjacency

    def _invalidate_cache(self) -> None:
        self._reverse_adjacency = None

    # --- serialisation ---

    def save(self, path: str | Path) -> None:
        """Write the graph to JSON.

        The format is compatible with existing ski-home / ski-home-api
        consumers: top-level "nodes", "edges", and "villages" keys.
        """
        path = Path(path)
        data = {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "villages": self.destinations,
        }
        with path.open("w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> "Graph":
        """Read a graph from JSON. Accepts both 'destinations' and 'villages' keys."""
        with Path(path).open() as f:
            data = json.load(f)

        g = cls()
        for n in data["nodes"]:
            node = Node(
                id=n["id"], lon=n["lon"], lat=n["lat"], elev=n["elev"],
                name=n.get("name", ""),
                destinations=n.get("villages", n.get("destinations", [])),
            )
            g.nodes[node.id] = node

        for e in data["edges"]:
            edge = Edge(
                from_id=e["from"], to_id=e["to"], type=e["type"],
                name=e.get("name", ""), cost_base=e["cost_base"],
                length_m=e.get("length_m", 0.0),
                elev_drop=e.get("elev_drop", 0.0),
                difficulty=e.get("difficulty") or "",
                lift_type=e.get("lift_type") or "",
                geometry=[tuple(c) for c in e.get("geometry", [])],
            )
            idx = len(g.edges)
            g.edges.append(edge)
            g.adjacency[edge.from_id].append(idx)

        g.destinations = data.get("villages", data.get("destinations", {}))
        return g
