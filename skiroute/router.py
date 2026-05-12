"""Dijkstra routing with a ski-specific cost model.

Three things make this not just a generic shortest-path:

  1. **Difficulty-adjusted cost.** A piste's edge cost in seconds is
     padded by a per-mode penalty (e.g. blacks become very expensive
     under prefer-easy). This steers the router toward routes a skier
     of a given level actually wants.

  2. **Lift-down handling.** Gondolas / cable cars / funiculars have an
     extra `lift_down_penalty` so the router prefers skiing. Under
     prefer-easy mode that penalty is discounted, so the router can
     opt to ride a gondola down rather than ski a black.

  3. **Most-skiing mode.** Subtracts `most_skiing_bonus_per_m` seconds
     per metre of descent on each run edge — the router maximises
     vertical metres rather than minimising time.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from skiroute.config import (
    Destination,
    DifficultyMode,
    RouteConfig,
    RouteObjective,
)
from skiroute.graph import Edge, Graph, Node


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Leg:
    """A consolidated stretch of the route (one named run, one lift, etc.)."""
    type: str
    name: str
    edges: list[Edge]
    from_node: Node
    to_node: Node
    length_m: float
    estimated_seconds: float
    difficulty: str = ""
    lift_type: str = ""
    elev_gain_m: float = 0.0

    def geometry(self) -> list[tuple[float, float]]:
        """Concatenated geometry across all edges in this leg."""
        coords: list[tuple[float, float]] = []
        for e in self.edges:
            if not e.geometry:
                continue
            if coords and tuple(e.geometry[0]) == coords[-1]:
                coords.extend(e.geometry[1:])
            else:
                coords.extend(e.geometry)
        return coords


@dataclass
class Route:
    """A complete route from start to goal."""
    legs: list[Leg]
    start_node: Node
    end_node: Node
    snap_distance_m: float = 0.0
    total_seconds: float = 0.0
    total_descent_m: float = 0.0

    @property
    def total_minutes(self) -> float:
        return self.total_seconds / 60.0

    def to_dict(self, include_geometry: bool = False) -> dict:
        legs = []
        for leg in self.legs:
            d = {
                "type": leg.type,
                "name": leg.name,
                "from": {"name": leg.from_node.name, "elev": leg.from_node.elev},
                "to": {"name": leg.to_node.name, "elev": leg.to_node.elev},
                "length_m": round(leg.length_m, 1),
                "estimated_seconds": round(leg.estimated_seconds, 1),
            }
            if leg.difficulty:
                d["difficulty"] = leg.difficulty
            if leg.lift_type:
                d["lift_type"] = leg.lift_type
                d["direction"] = "down" if leg.type == "lift_down" else "up"
            if leg.elev_gain_m:
                d["elev_gain"] = round(leg.elev_gain_m, 1)
            if include_geometry:
                d["geometry"] = [list(c) for c in leg.geometry()]
            legs.append(d)
        return {
            "from": {
                "name": self.start_node.name,
                "lon": self.start_node.lon,
                "lat": self.start_node.lat,
                "elev": self.start_node.elev,
                "snap_distance_m": round(self.snap_distance_m, 1),
            },
            "to": {
                "name": self.end_node.name,
                "elev": self.end_node.elev,
            },
            "summary": {
                "legs": len(legs),
                "runs": sum(1 for l in legs if l["type"] in ("run", "connection")),
                "lifts": sum(1 for l in legs if l["type"] in ("lift", "lift_down")),
                "estimated_minutes": round(self.total_seconds / 60),
                "vertical_m": round(self.total_descent_m, 1),
            },
            "legs": legs,
        }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def route(
    graph: Graph,
    start: tuple[float, float] | int,
    end: tuple[float, float] | int,
    mode: DifficultyMode = "any-piste",
    objective: RouteObjective = "fastest",
    config: RouteConfig | None = None,
) -> Route | None:
    """Route from a start coordinate (or node ID) to an end coordinate (or node ID).

    Returns None if no route exists.
    """
    config = config or RouteConfig()
    start_node, snap_dist = _resolve_start(graph, start)
    end_node, _ = _resolve_start(graph, end)
    targets = {end_node.id}

    path, _err = _dijkstra(graph, start_node.id, targets, mode, objective, config)
    if path is None:
        return None
    return _build_route(graph, path, start_node, snap_dist)


def route_home(
    graph: Graph,
    start: tuple[float, float] | int,
    destinations: Sequence[str | Destination] | None = None,
    mode: DifficultyMode = "any-piste",
    objective: RouteObjective = "fastest",
    via: int | str | None = None,
    config: RouteConfig | None = None,
) -> Route | None:
    """Route to the nearest of N destinations.

    Args:
        graph: The graph to route on.
        start: GPS coordinate (lon, lat) or node ID.
        destinations: Which destinations are acceptable goals. Either:
            - A list of destination names (already tagged on the graph), or
            - A list of Destination objects (tagging is done in-place), or
            - None: use all destinations already on the graph.
        mode: Difficulty preference.
        objective: "fastest" or "most-skiing".
        via: Optional waypoint — node ID or substring of node name.

    Returns:
        Route or None if unreachable.
    """
    config = config or RouteConfig()
    start_node, snap_dist = _resolve_start(graph, start)

    target_names = _resolve_destinations(graph, destinations)
    target_nodes: set[int] = set()
    for name in target_names:
        target_nodes |= graph.nodes_for_destination(name)
    if not target_nodes:
        return None

    if via is not None:
        via_id = _resolve_via(graph, via)
        if via_id is None:
            return None
        leg_a, _ = _dijkstra(graph, start_node.id, {via_id}, mode, objective, config)
        if leg_a is None:
            return None
        leg_b, _ = _dijkstra(graph, via_id, target_nodes, mode, objective, config)
        if leg_b is None:
            return None
        path = leg_a + leg_b
    else:
        path, _ = _dijkstra(graph, start_node.id, target_nodes, mode, objective, config)
        if path is None:
            return None

    # Extend through the destination so the route ends at the village base
    final_dest = _which_destination(graph, path[-1].to_id, target_names)
    if final_dest:
        path = _extend_through_destination(graph, path, final_dest)

    return _build_route(graph, path, start_node, snap_dist)


# ---------------------------------------------------------------------------
# Dijkstra core
# ---------------------------------------------------------------------------


def _dijkstra(
    graph: Graph,
    from_id: int,
    target_nodes: set[int],
    mode: DifficultyMode,
    objective: RouteObjective,
    config: RouteConfig,
) -> tuple[list[Edge] | None, str | None]:
    """Dijkstra with the ski cost model. Lazy dist init for large graphs."""
    cost_fn = _make_cost_fn(mode, objective, config)

    # Lazy dist: only initialised for nodes actually touched.
    dist: dict[int, float] = {from_id: 0.0}
    prev: dict[int, int] = {}
    prev_edge: dict[int, Edge] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, from_id)]

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)

        if u in target_nodes:
            return _reconstruct(prev, prev_edge, u), None

        for edge_idx in graph.adjacency.get(u, ()):
            edge = graph.edges[edge_idx]
            v = edge.to_id
            if v in visited:
                continue
            nd = d + cost_fn(edge)
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                prev_edge[v] = edge
                heapq.heappush(heap, (nd, v))

    return None, "No route found"


def _make_cost_fn(mode: DifficultyMode, objective: RouteObjective, config: RouteConfig):
    """Return a closure edge → cost that bakes in the mode and objective."""
    penalties = config.difficulty_penalty.get(
        mode, config.difficulty_penalty["any-piste"])
    lift_down_discount = (
        config.lift_down_penalty * config.lift_down_easy_discount
        if mode == "prefer-easy" else 0.0
    )
    most_skiing_bonus = (
        config.most_skiing_bonus_per_m if objective == "most-skiing" else 0.0
    )

    def cost(edge: Edge) -> float:
        c = edge.cost_base
        if edge.difficulty and edge.type in ("run", "connection"):
            c += penalties.get(edge.difficulty, 0)
        if lift_down_discount and edge.type == "lift_down":
            c -= lift_down_discount
        if most_skiing_bonus and edge.type in ("run", "connection"):
            c -= edge.elev_drop * most_skiing_bonus
        return max(c, 1.0)

    return cost


def _reconstruct(prev: dict[int, int], prev_edge: dict[int, Edge], u: int) -> list[Edge]:
    path: list[Edge] = []
    cur = u
    while cur in prev_edge:
        path.append(prev_edge[cur])
        cur = prev[cur]
    path.reverse()
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_start(graph: Graph, start: tuple[float, float] | int) -> tuple[Node, float]:
    if isinstance(start, int):
        return graph.nodes[start], 0.0
    lon, lat = start
    node, dist = graph.find_nearest_node(lon, lat)
    return node, dist


def _resolve_via(graph: Graph, via: int | str) -> int | None:
    if isinstance(via, int):
        return via if via in graph.nodes else None
    matches = graph.find_nodes_by_name(via)
    return matches[0].id if matches else None


def _resolve_destinations(
    graph: Graph, destinations: Sequence[str | Destination] | None,
) -> list[str]:
    if destinations is None:
        return list(graph.destinations.keys())

    names: list[str] = []
    for d in destinations:
        if isinstance(d, str):
            names.append(d)
        else:
            # Destination object — tag the graph if not already
            if d.name not in graph.destinations:
                _tag_one_destination(graph, d)
            names.append(d.name)
    return names


def _tag_one_destination(graph: Graph, d: Destination) -> None:
    from skiroute.geometry import haversine
    for node in graph.nodes.values():
        if haversine(node.lon, node.lat, d.lon, d.lat) > d.radius_m:
            continue
        if d.elev is not None and (node.elev - d.elev) > d.elev_tolerance_m:
            continue
        if d.name not in node.destinations:
            node.destinations.append(d.name)
    graph.destinations[d.name] = {"lat": d.lat, "lon": d.lon, "elev": d.elev or 0.0}


def _which_destination(graph: Graph, node_id: int, names: Iterable[str]) -> str | None:
    node = graph.nodes.get(node_id)
    if not node:
        return None
    for n in names:
        if n in node.destinations:
            return n
    return None


def _extend_through_destination(graph: Graph, path: list[Edge], destination: str) -> list[Edge]:
    """After reaching the first node tagged with this destination, keep
    descending via runs/connections within the destination so the route
    finishes at the village base, not on the slopes above it."""
    if not path:
        return path
    cursor = path[-1].to_id
    while True:
        best_edge, best_drop = None, 0.0
        for e in graph.adjacent_edges(cursor):
            if e.type not in ("run", "connection"):
                continue
            dest_node = graph.nodes[e.to_id]
            if destination not in dest_node.destinations:
                continue
            if dest_node.elev < graph.nodes[cursor].elev and e.elev_drop > best_drop:
                best_edge = e
                best_drop = e.elev_drop
        if best_edge is None:
            break
        path.append(best_edge)
        cursor = best_edge.to_id
    return path


def _build_route(graph: Graph, path: list[Edge], start_node: Node, snap_dist: float) -> Route:
    legs = _consolidate_legs(graph, path)
    total_seconds = sum(e.cost_base for e in path)
    total_descent = sum(e.elev_drop for e in path if e.type in ("run", "connection"))
    end_node = graph.nodes[path[-1].to_id] if path else start_node
    return Route(
        legs=legs,
        start_node=start_node,
        end_node=end_node,
        snap_distance_m=snap_dist,
        total_seconds=total_seconds,
        total_descent_m=total_descent,
    )


def _consolidate_legs(graph: Graph, path: list[Edge]) -> list[Leg]:
    if not path:
        return []
    legs: list[Leg] = []
    cur_edges = [path[0]]
    cur_type = path[0].type
    cur_name = path[0].name

    def flush() -> None:
        first, last = cur_edges[0], cur_edges[-1]
        from_n = graph.nodes[first.from_id]
        to_n = graph.nodes[last.to_id]
        leg = Leg(
            type=cur_type, name=cur_name, edges=list(cur_edges),
            from_node=from_n, to_node=to_n,
            length_m=sum(e.length_m for e in cur_edges),
            estimated_seconds=sum(e.cost_base for e in cur_edges),
            difficulty=first.difficulty if cur_type in ("run", "connection") else "",
            lift_type=first.lift_type if cur_type in ("lift", "lift_down") else "",
            elev_gain_m=max(0.0, to_n.elev - from_n.elev) if cur_type == "skate" else 0.0,
        )
        if cur_type == "skate":
            leg.name = f"Skate {leg.length_m:.0f}m"
        legs.append(leg)

    for edge in path[1:]:
        same_run = (edge.name == cur_name and edge.type == cur_type
                    and edge.type in ("run", "connection"))
        same_skate = edge.type == "skate" and cur_type == "skate"
        if same_run or same_skate:
            cur_edges.append(edge)
        else:
            flush()
            cur_edges = [edge]
            cur_type = edge.type
            cur_name = edge.name
    flush()
    return legs
