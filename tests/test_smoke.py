"""Smoke tests for skiroute.

These tests load the bundled Tignes graph.json from examples/data/
and verify basic routing works end-to-end. They do NOT require the
OpenSkiData GeoPackage and are safe to run anywhere.
"""

from pathlib import Path

import pytest

import skiroute


GRAPH_PATH = Path(__file__).parent.parent / "examples" / "data" / "tignes.json"


@pytest.fixture(scope="module")
def graph():
    if not GRAPH_PATH.exists():
        pytest.skip(f"No graph fixture at {GRAPH_PATH}")
    return skiroute.Graph.load(GRAPH_PATH)


def test_graph_loads(graph):
    assert len(graph.nodes) > 100
    assert len(graph.edges) > 500
    assert graph.destinations


def test_point_to_point(graph):
    # Solaise area → Le Lac
    n_start, _ = graph.find_nearest_node(6.985, 45.452)
    n_end, _ = graph.find_nearest_node(6.907, 45.468)
    result = skiroute.route(graph, n_start.id, n_end.id, mode="any-piste")
    assert result is not None
    assert result.total_seconds > 0
    assert len(result.legs) > 0


def test_route_home(graph):
    result = skiroute.route_home(
        graph,
        start=(6.985, 45.452),
        destinations=["Tignes Le Lac"],
        mode="any-piste",
    )
    assert result is not None
    assert result.end_node is not None


def test_difficulty_modes_differ(graph):
    """prefer-easy should take longer than any-piste (or equal — never shorter)."""
    easy = skiroute.route_home(graph, start=(6.985, 45.452),
                               destinations=["Tignes Le Lac"], mode="prefer-easy")
    any_piste = skiroute.route_home(graph, start=(6.985, 45.452),
                                     destinations=["Tignes Le Lac"], mode="any-piste")
    assert easy and any_piste
    assert easy.total_seconds >= any_piste.total_seconds


def test_most_skiing_yields_more_vertical(graph):
    fast = skiroute.route_home(graph, start=(6.985, 45.452),
                               destinations=["Tignes Le Lac"],
                               mode="any-piste", objective="fastest")
    most = skiroute.route_home(graph, start=(6.985, 45.452),
                               destinations=["Tignes Le Lac"],
                               mode="any-piste", objective="most-skiing")
    assert fast and most
    assert most.total_descent_m >= fast.total_descent_m
