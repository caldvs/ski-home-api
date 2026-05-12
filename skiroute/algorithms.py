"""Graph search algorithms — Dijkstra, A*, bidirectional Dijkstra.

These are deliberately *generic*: they take a cost function and (for A*)
a heuristic. Ski-specific behaviour lives in `router.py`, which composes
these algorithms with a cost function that knows about difficulty modes,
lift-down penalties, and the most-skiing bonus.

All three return the same shape: `(path, stats)` where path is a list
of edges or None, and stats reports how much work was done.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable

from skiroute.geometry import haversine
from skiroute.graph import Edge, Graph


CostFn = Callable[[Edge], float]
HeuristicFn = Callable[[int], float]


@dataclass
class SearchStats:
    """How much work an algorithm did. Useful for visualisation + benchmarks."""
    nodes_visited: int = 0
    edges_relaxed: int = 0
    heap_pushes: int = 0
    visited_set: set[int] = field(default_factory=set)  # for animation
    # For bidirectional: nodes visited by the backward search
    backward_visited_set: set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Dijkstra
# ---------------------------------------------------------------------------


def dijkstra(
    graph: Graph,
    source: int,
    targets: Iterable[int] | int,
    cost_fn: CostFn,
    record_visited: bool = False,
) -> tuple[list[Edge] | None, SearchStats]:
    """Single-source shortest path with the given cost function.

    `targets` may be a single node id or a set of acceptable goals.
    Stops as soon as any target is settled.
    """
    if isinstance(targets, int):
        target_set = {targets}
    else:
        target_set = set(targets)

    stats = SearchStats()
    dist: dict[int, float] = {source: 0.0}
    prev: dict[int, int] = {}
    prev_edge: dict[int, Edge] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, source)]
    stats.heap_pushes = 1

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        stats.nodes_visited += 1
        if record_visited:
            stats.visited_set.add(u)

        if u in target_set:
            return _reconstruct(prev, prev_edge, u), stats

        for edge_idx in graph.adjacency.get(u, ()):
            edge = graph.edges[edge_idx]
            v = edge.to_id
            stats.edges_relaxed += 1
            if v in visited:
                continue
            nd = d + cost_fn(edge)
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                prev_edge[v] = edge
                heapq.heappush(heap, (nd, v))
                stats.heap_pushes += 1

    return None, stats


# ---------------------------------------------------------------------------
# A*
# ---------------------------------------------------------------------------


def astar(
    graph: Graph,
    source: int,
    target: int,
    cost_fn: CostFn,
    heuristic_fn: HeuristicFn,
    record_visited: bool = False,
) -> tuple[list[Edge] | None, SearchStats]:
    """A* search with a single target.

    For optimality the heuristic must be admissible (never overestimate
    the true remaining cost). For the ski cost model with mode='any-piste'
    and objective='fastest', `geographic_heuristic` below is admissible.
    Under 'most-skiing' it loses optimality (some edges have negative cost)
    but tends to return very similar routes much faster.
    """
    stats = SearchStats()
    g_score: dict[int, float] = {source: 0.0}
    prev: dict[int, int] = {}
    prev_edge: dict[int, Edge] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(heuristic_fn(source), source)]
    stats.heap_pushes = 1

    while heap:
        _f, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        stats.nodes_visited += 1
        if record_visited:
            stats.visited_set.add(u)

        if u == target:
            return _reconstruct(prev, prev_edge, u), stats

        gu = g_score[u]
        for edge_idx in graph.adjacency.get(u, ()):
            edge = graph.edges[edge_idx]
            v = edge.to_id
            stats.edges_relaxed += 1
            if v in visited:
                continue
            tentative_g = gu + cost_fn(edge)
            if tentative_g < g_score.get(v, float("inf")):
                g_score[v] = tentative_g
                prev[v] = u
                prev_edge[v] = edge
                f = tentative_g + heuristic_fn(v)
                heapq.heappush(heap, (f, v))
                stats.heap_pushes += 1

    return None, stats


def geographic_heuristic(graph: Graph, target_id: int, max_speed_ms: float = 8.0) -> HeuristicFn:
    """Admissible heuristic for time-based costs over geographic edges.

    Returns the great-circle distance from a node to the target divided
    by max ski speed — a lower bound on the time it could possibly take.
    """
    tgt = graph.nodes[target_id]
    tlon, tlat = tgt.lon, tgt.lat

    def h(node_id: int) -> float:
        n = graph.nodes[node_id]
        return haversine(n.lon, n.lat, tlon, tlat) / max_speed_ms

    return h


# ---------------------------------------------------------------------------
# Bidirectional Dijkstra
# ---------------------------------------------------------------------------


def bidirectional_dijkstra(
    graph: Graph,
    source: int,
    target: int,
    cost_fn: CostFn,
    record_visited: bool = False,
) -> tuple[list[Edge] | None, SearchStats]:
    """Two-frontier Dijkstra meeting in the middle.

    Searches forward from source on the regular graph AND backward from
    target on the reversed graph. Terminates when the sum of the two
    frontier minimums exceeds the best known meeting cost — at that
    point no future meeting can improve on the current best.

    Works for any non-negative cost function. Returns the same optimal
    path as plain Dijkstra (with the same cost) but typically visits
    ~half as many nodes on geographic graphs.
    """
    reverse_adj = graph.reverse_adjacency()

    stats = SearchStats()

    # Forward state
    fwd_dist: dict[int, float] = {source: 0.0}
    fwd_prev: dict[int, int] = {}
    fwd_prev_edge: dict[int, Edge] = {}
    fwd_settled: dict[int, float] = {}
    fwd_heap: list[tuple[float, int]] = [(0.0, source)]

    # Backward state (distance from target travelling backwards)
    bwd_dist: dict[int, float] = {target: 0.0}
    bwd_next: dict[int, int] = {}        # node → next node toward target
    bwd_next_edge: dict[int, Edge] = {}  # node → edge to traverse forward
    bwd_settled: dict[int, float] = {}
    bwd_heap: list[tuple[float, int]] = [(0.0, target)]

    stats.heap_pushes = 2

    best_cost = float("inf")
    meeting_node: int | None = None

    def consider_meeting(node: int, total: float) -> None:
        """Check whether a candidate meeting cost improves the best."""
        nonlocal best_cost, meeting_node
        if total < best_cost:
            best_cost = total
            meeting_node = node

    while fwd_heap or bwd_heap:
        # Termination: if min(fwd_top) + min(bwd_top) >= best_cost, no future
        # meeting can improve. Both frontiers must satisfy this to terminate.
        f_top = fwd_heap[0][0] if fwd_heap else float("inf")
        b_top = bwd_heap[0][0] if bwd_heap else float("inf")
        if f_top + b_top >= best_cost:
            break

        # Expand the side whose frontier minimum is smaller.
        expand_forward = f_top <= b_top

        if expand_forward and fwd_heap:
            d, u = heapq.heappop(fwd_heap)
            if u in fwd_settled:
                continue
            fwd_settled[u] = d
            stats.nodes_visited += 1
            if record_visited:
                stats.visited_set.add(u)
            # Meeting check at settlement
            if u in bwd_dist:
                consider_meeting(u, d + bwd_dist[u])

            for edge_idx in graph.adjacency.get(u, ()):
                edge = graph.edges[edge_idx]
                v = edge.to_id
                stats.edges_relaxed += 1
                if v in fwd_settled:
                    continue
                nd = d + cost_fn(edge)
                if nd < fwd_dist.get(v, float("inf")):
                    fwd_dist[v] = nd
                    fwd_prev[v] = u
                    fwd_prev_edge[v] = edge
                    heapq.heappush(fwd_heap, (nd, v))
                    stats.heap_pushes += 1
                    # Meeting check during relaxation — catches paths
                    # where v has been discovered by the backward search
                    # but not yet settled. Important for optimality.
                    if v in bwd_dist:
                        consider_meeting(v, nd + bwd_dist[v])
        elif bwd_heap:
            d, u = heapq.heappop(bwd_heap)
            if u in bwd_settled:
                continue
            bwd_settled[u] = d
            stats.nodes_visited += 1
            if record_visited:
                stats.backward_visited_set.add(u)
            if u in fwd_dist:
                consider_meeting(u, fwd_dist[u] + d)

            for edge_idx in reverse_adj.get(u, ()):
                edge = graph.edges[edge_idx]
                v = edge.from_id
                stats.edges_relaxed += 1
                if v in bwd_settled:
                    continue
                nd = d + cost_fn(edge)
                if nd < bwd_dist.get(v, float("inf")):
                    bwd_dist[v] = nd
                    bwd_next[v] = u
                    bwd_next_edge[v] = edge
                    heapq.heappush(bwd_heap, (nd, v))
                    stats.heap_pushes += 1
                    if v in fwd_dist:
                        consider_meeting(v, fwd_dist[v] + nd)

    if meeting_node is None:
        return None, stats

    # Reconstruct: forward source → meeting + backward meeting → target
    path_a = _reconstruct(fwd_prev, fwd_prev_edge, meeting_node)
    path_b: list[Edge] = []
    cur = meeting_node
    while cur in bwd_next_edge:
        path_b.append(bwd_next_edge[cur])
        cur = bwd_next[cur]
    return path_a + path_b, stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reconstruct(prev: dict[int, int], prev_edge: dict[int, Edge], u: int) -> list[Edge]:
    path: list[Edge] = []
    cur = u
    while cur in prev_edge:
        path.append(prev_edge[cur])
        cur = prev[cur]
    path.reverse()
    return path
