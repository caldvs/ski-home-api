"""Smoke test for the contraction-hierarchies module.

This is the lightest possible coverage that proves the CH preprocessing
+ query path works end-to-end and agrees with plain Dijkstra on a
sampled set of OD pairs. CH gives the same shortest paths as Dijkstra
by construction — if it doesn't, the preprocessing is broken.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

from skiroute import algorithms, contraction_hierarchies as ch
from skiroute.config import RouteConfig
from skiroute.router import _make_cost_fn

sys.path.insert(0, str(Path(__file__).parent))
from coverage_scenarios import GRAPH_PATH, load_graph  # noqa: E402


@pytest.fixture(scope="module")
def graph():
    if not GRAPH_PATH.exists():
        pytest.skip(f"No graph fixture at {GRAPH_PATH}")
    return load_graph()


@pytest.fixture(scope="module")
def cost_fn():
    return _make_cost_fn("any-piste", "fastest", RouteConfig())


@pytest.fixture(scope="module")
def ch_graph(graph, cost_fn):
    """Preprocess the Tignes graph into a CHGraph. Shared by the queries below."""
    return ch.preprocess(graph, cost_fn, verbose=False)


def _path_cost(path, cost_fn):
    if path is None:
        return None
    return sum(cost_fn(e) for e in path)


def test_ch_preprocess_succeeds(ch_graph):
    """Building the CHGraph completes and has plausible structure."""
    assert ch_graph is not None
    # CH augments the graph with shortcut edges; expect at least *some*
    # shortcuts on a real resort graph.
    assert len(ch_graph.edges) >= 1


def test_ch_query_returns_same_cost_as_dijkstra(graph, cost_fn, ch_graph):
    """For a sample of OD pairs, CH and Dijkstra must agree on path cost.

    They may produce different edges when ties exist, but the total cost
    must match (CH is exact, not heuristic)."""
    rng = random.Random(0)
    node_ids = list(graph.nodes)
    sampled = rng.sample(node_ids, k=min(15, len(node_ids)))
    pairs = [(s, t) for s in sampled[:5] for t in sampled[5:10] if s != t]

    mismatches: list[str] = []
    tried = 0
    for s, t in pairs:
        dj_path, _ = algorithms.dijkstra(graph, s, t, cost_fn)
        ch_path, _ = ch.query(ch_graph, s, t)
        dj_cost = _path_cost(dj_path, cost_fn)
        ch_cost = _path_cost(ch_path, cost_fn)

        # Both should agree on reachability
        if (dj_path is None) != (ch_path is None):
            mismatches.append(f"{s}→{t}: reachability disagrees (Dijkstra={dj_path is not None}, CH={ch_path is not None})")
            continue
        if dj_path is None:
            continue
        tried += 1

        # Compare costs (cost-equal is the CH guarantee; edge-equality isn't)
        if abs(dj_cost - ch_cost) > 0.5:  # half-second tolerance for rounding
            mismatches.append(f"{s}→{t}: Dijkstra={dj_cost:.1f}s, CH={ch_cost:.1f}s, diff={ch_cost-dj_cost:+.1f}s")

    assert tried >= 5, f"Too few reachable pairs sampled ({tried}); test isn't exercising CH"
    assert not mismatches, (
        f"{len(mismatches)} CH/Dijkstra mismatches in {tried} pairs:\n  "
        + "\n  ".join(mismatches[:5])
    )
