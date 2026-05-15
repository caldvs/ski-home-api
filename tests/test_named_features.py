"""L2 — hand-written named-feature scenarios.

Each test is a plain-English claim about the resort: "from the bottom
of the Fresse lift you should be able to reach Tignes Le Lac". Tests
read like a checklist; when a routing bug is discovered, add a new
test here so the regression is locked in.

These complement the matrix-based coverage tests in
``test_resort_coverage.py``: those prove *general* reachability, these
prove *specific* reachability with named features.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import skiroute

sys.path.insert(0, str(Path(__file__).parent))
from coverage_scenarios import (  # noqa: E402
    GRAPH_PATH,
    feature_endpoints,
    load_graph,
    named_features,
)


@pytest.fixture(scope="module")
def graph():
    if not GRAPH_PATH.exists():
        pytest.skip(f"No graph fixture at {GRAPH_PATH}")
    return load_graph()


# ---------------------------------------------------------------------------
# Small helpers — push the "find the named thing in the graph" detail out
# of the test body so the assertions read like the user's prose.
# ---------------------------------------------------------------------------


def _lift_endpoints(graph, lift_name: str) -> tuple[int, int]:
    """Return (top_node_id, bottom_node_id) of a named lift."""
    _, lifts = named_features(graph)
    matches = [k for k in lifts if k.lower() == lift_name.lower()]
    assert matches, f"No lift named '{lift_name}' in graph. Known: {sorted(lifts)[:8]} …"
    return feature_endpoints(graph, lifts[matches[0]])


def _run_endpoints(graph, run_name: str) -> tuple[int, int]:
    """Return (top_node_id, bottom_node_id) of a named run."""
    runs, _ = named_features(graph)
    matches = [k for k in runs if k.lower() == run_name.lower()]
    assert matches, f"No run named '{run_name}' in graph. Try: {sorted(runs)[:8]} …"
    return feature_endpoints(graph, runs[matches[0]])


def _leg_names(route) -> list[str]:
    return [leg.name for leg in route.legs]


# ---------------------------------------------------------------------------
# Fresse → Bollin → Mont Blanc (the user's worked example)
# ---------------------------------------------------------------------------


def test_fresse_bottom_can_reach_tignes_le_lac(graph):
    """From the bottom of the Fresse lift, route home to Tignes Le Lac."""
    _, fresse_bottom = _lift_endpoints(graph, "Fresse")
    r = skiroute.route_home(graph, fresse_bottom, ["Tignes Le Lac"], mode="any-piste")
    assert r is not None, "No route from Fresse bottom to Tignes Le Lac"
    assert r.total_seconds > 0
    assert len(r.legs) >= 1


def test_fresse_bottom_can_reach_mont_blanc_run(graph):
    """The user's specific claim: Fresse → Bollin chain → Mont Blanc green run.

    Asserts that, starting from the bottom of Fresse, a route exists
    that *ends* on (or passes through) the Mont Blanc named run."""
    _, fresse_bottom = _lift_endpoints(graph, "Fresse")
    mb_top, mb_bottom = _run_endpoints(graph, "Mont Blanc")
    r = skiroute.route(graph, fresse_bottom, mb_top, mode="any-piste")
    assert r is not None, "Cannot find a route from Fresse bottom to top of Mont Blanc run"


def test_route_into_mont_blanc_uses_a_lift_chain(graph):
    """Mont Blanc is on the Val d'Isère side; from Tignes Val Claret you
    can only reach it by riding at least one lift. The route should
    contain ≥ 1 lift leg."""
    val_claret_node = next(
        (n for n in graph.nodes.values() if "Tignes Val Claret" in n.destinations),
        None,
    )
    assert val_claret_node, "No node tagged with 'Tignes Val Claret'"
    mb_top, _ = _run_endpoints(graph, "Mont Blanc")
    r = skiroute.route(graph, val_claret_node.id, mb_top, mode="any-piste")
    assert r is not None, "Val Claret cannot reach Mont Blanc"
    lifts = [leg for leg in r.legs if leg.type == "lift"]
    assert lifts, f"Expected at least one lift leg, got: {_leg_names(r)}"


# ---------------------------------------------------------------------------
# Mode contracts on a real, named example
# ---------------------------------------------------------------------------


def test_prefer_easy_does_not_pick_blacks_when_avoidable(graph):
    """From the top of Tovière (a well-connected lift) to Tignes Le Lac,
    prefer-easy should be able to find a route with no advanced / expert
    runs — there are well-known blue alternatives."""
    toviere_top, _ = _lift_endpoints(graph, "Tovière")
    r = skiroute.route_home(graph, toviere_top, ["Tignes Le Lac"], mode="prefer-easy")
    assert r is not None
    blacks = [leg.name for leg in r.legs if leg.difficulty in ("advanced", "expert")]
    assert not blacks, (
        f"prefer-easy picked black runs from Tovière → Le Lac: {blacks}. "
        f"Full leg names: {_leg_names(r)}"
    )


def test_most_skiing_drops_more_than_fastest_from_grande_motte(graph):
    """From the top of the Grande Motte (highest point in Tignes),
    most-skiing should descend ≥ as many vertical metres as fastest."""
    grande_motte_top, _ = _lift_endpoints(graph, "Grande Motte")
    fast = skiroute.route_home(
        graph, grande_motte_top, ["Tignes Val Claret"],
        mode="any-piste", objective="fastest",
    )
    most = skiroute.route_home(
        graph, grande_motte_top, ["Tignes Val Claret"],
        mode="any-piste", objective="most-skiing",
    )
    assert fast is not None and most is not None
    assert most.total_descent_m + 1 >= fast.total_descent_m, (
        f"most-skiing dropped {most.total_descent_m:.0f}m; "
        f"fastest dropped {fast.total_descent_m:.0f}m"
    )


# ---------------------------------------------------------------------------
# Cross-valley connectivity (the canonical multi-leg journey)
# ---------------------------------------------------------------------------


def test_val_claret_can_reach_val_disere_centre(graph):
    """Tignes Val Claret ↔ Val d'Isère Centre is the canonical inter-valley
    journey: requires at least one lift to cross the col."""
    start = next(
        (n for n in graph.nodes.values() if "Tignes Val Claret" in n.destinations),
        None,
    )
    assert start
    r = skiroute.route_home(graph, start.id, ["Val d'Isere Centre"], mode="any-piste")
    assert r is not None, "Val Claret cannot route to Val d'Isère Centre"
    lifts = [leg for leg in r.legs if leg.type == "lift"]
    assert lifts, "Cross-valley route had no lift — suspicious"


def test_le_fornet_can_reach_les_brevieres(graph):
    """The two valley endpoints (Le Fornet at top of Val d'Isère, Les
    Brévières at bottom of Tignes) must be connected — the longest
    possible journey on the home-routing graph."""
    start = next(
        (n for n in graph.nodes.values() if "Val d'Isere Le Fornet" in n.destinations),
        None,
    )
    assert start, "No node tagged with Val d'Isere Le Fornet"
    r = skiroute.route_home(graph, start.id, ["Tignes Les Brevieres"], mode="any-piste")
    assert r is not None, "Le Fornet cannot reach Les Brévières — graph is severed"
    assert r.total_minutes < 240, (
        f"Cross-resort journey > 4 hours ({r.total_minutes:.0f} min) — "
        f"cost model has likely regressed"
    )


# ---------------------------------------------------------------------------
# Snap correctness — coordinate-based starts
# ---------------------------------------------------------------------------


def test_route_home_from_node_already_in_village(graph):
    """Regression: previously crashed with IndexError when the start
    node was itself tagged with the target destination. Should now
    return a valid (possibly zero-leg) Route ending at the village."""
    home_node = next(
        (n for n in graph.nodes.values() if "Tignes Le Lac" in n.destinations),
        None,
    )
    assert home_node, "No node tagged with Tignes Le Lac in this graph"
    r = skiroute.route_home(graph, home_node.id, ["Tignes Le Lac"], mode="any-piste")
    assert r is not None
    assert r.start_node.id == home_node.id
    # End node should still be tagged with the destination
    assert "Tignes Le Lac" in r.end_node.destinations


def test_known_gps_snaps_close_enough(graph):
    """A coordinate dropped *near* (offset ~100 m from) the Tovière base
    station should snap onto a node within 200 m. Anything further means
    the station node is missing or mis-located in the graph."""
    # Tovière base lon/lat = 6.9072, 45.4682 — nudge a fraction east+south
    # so we're testing snap behaviour on a coordinate that didn't *literally*
    # come from the graph itself.
    node, dist = graph.find_nearest_node(6.9080, 45.4675)
    assert dist < 200, f"Tovière-base snap distance = {dist:.0f} m, expected < 200 m"
    assert node is not None
    assert "Tovière" in node.name or node.elev > 2000  # plausibly the lift area
