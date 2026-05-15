"""L0 + L1 resort-coverage tests.

  L0 — **Graph audit**. Static, no routing-mode dimension. Asks: from
       every realistic starting point (top of every named piste/lift),
       can the router find a way home to *at least one* village in the
       permissive ``any-piste`` mode? Failures here indicate a graph
       hole, not a cost-model bug.

       Also includes **whole-graph connectivity** checks: Tarjan SCC
       analysis confirms that every non-orphan node lives in the single
       giant component (so every node reaches every other), and a full
       N×N Dijkstra matrix proves all-pairs reachability directly.

  L1 — **Routing matrix**. The full origin × village × mode product
       under the default ``fastest`` objective. Asserts everything is
       reachable; tolerates a small number of ``prefer-easy`` failures
       (some experts-only nodes legitimately cannot reach every village
       without a black run).

Both are run via pytest; both exercise the library directly. The
standalone HTML report (``coverage_report.py``) extends this with the
``most-skiing`` objective and produces a visualisation.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

import skiroute
from skiroute.router import _dijkstra
from skiroute.config import RouteConfig

sys.path.insert(0, str(Path(__file__).parent))
from coverage_scenarios import (  # noqa: E402
    GRAPH_PATH,
    Origin,
    build_origins,
    load_graph,
    villages,
)


# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------

# In prefer-easy mode some origins might fail to reach every village if
# the only exits are blacks. Empirically (after all current overrides)
# the rate is 0%; we leave a 2% headroom for future graph changes.
# Increase only if you've investigated and confirmed the failure is
# real-world correct (i.e. a black-only feature with no plausible exit).
MAX_PREFER_EASY_FAILURE_FRACTION = 0.02

# We sample this many origins for the (expensive) cross-mode contract tests.
CONTRACT_SAMPLE_SIZE = 60

# Features that are KNOWN to be unreachable in standard modes — they
# are off-piste / freeride / unnamed graph artefacts with no piste
# exit. Listing them here turns the L0/L1 tests into snapshot checks:
# we fail when a *new* feature becomes orphaned, but accept these
# pre-known cases. If you fix the graph for one of these (e.g. by
# adding a skating edge), remove it from this set.
KNOWN_ORPHAN_RUNS: set[str] = {
    "Col Pers",                  # itinéraire / off-piste, no marked exit
    "couloir de grande balme",   # expert couloir (petite balme removed via override)
    "Variante Col",              # short variant near Col Pers
}

# Node *names* that are known to live outside the giant SCC.
# Snapshot semantics: fail when an unexpected pocket appears.
#
# Each remaining pocket is intentional or pending:
#   - Col Pers (top × 2, bottom)                        — itinéraire / off-piste
#   - couloir de grande balme bottom                    — expert couloir
#   - Couloir de petite balme top                       — terminus of grande balme
#   - Couloir de la Table d'orientation top             — expert couloir
#   - Face de Bellevarde top                            — the user has flagged this
#     as needing manual connection via the editor; will come off this list
#     once the override lands.
KNOWN_POCKET_NODE_NAMES: set[str] = {
    "Col Pers top",
    "Col Pers bottom",
    "couloir de grande balme bottom",
    "Couloir de petite balme top",
    "Couloir de la Table d'orientation top",
    "Face de Bellevarde top",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph() -> skiroute.Graph:
    if not GRAPH_PATH.exists():
        pytest.skip(f"No graph fixture at {GRAPH_PATH}")
    return load_graph()


@pytest.fixture(scope="module")
def origins(graph) -> list[Origin]:
    return build_origins(graph)


@pytest.fixture(scope="module")
def village_list(graph) -> list[str]:
    return villages(graph)


# ---------------------------------------------------------------------------
# L0 — graph audit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tarjan_sccs(graph: skiroute.Graph) -> list[list[int]]:
    """Iterative Tarjan SCC. Returns components sorted by size descending."""
    adj: dict[int, list[int]] = defaultdict(list)
    for e in graph.edges:
        adj[e.from_id].append(e.to_id)

    index_counter = [0]
    idx: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    sccs: list[list[int]] = []

    for root in graph.nodes:
        if root in idx:
            continue
        # Iterative DFS — each frame is (node, iterator over successors).
        idx[root] = index_counter[0]
        low[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(adj.get(root, [])))]
        while work:
            v, it = work[-1]
            try:
                w = next(it)
                if w not in idx:
                    idx[w] = index_counter[0]
                    low[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj.get(w, []))))
                elif w in on_stack:
                    low[v] = min(low[v], idx[w])
            except StopIteration:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == idx[v]:
                    comp: list[int] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(comp)

    sccs.sort(key=len, reverse=True)
    return sccs


def _dijkstra_reachable_set(graph: skiroute.Graph, source_id: int) -> set[int]:
    """All node IDs reachable from `source_id` via forward edges in any-piste mode."""
    config = RouteConfig()
    # Trick: use a target set that never matches so Dijkstra explores the
    # whole reachable subgraph, then return every node it visited.
    # The internal _dijkstra returns prev/prev_edge maps via reconstruction
    # — we instead replicate the BFS here to get the visited set.
    from heapq import heappush, heappop
    dist: dict[int, float] = {source_id: 0.0}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, source_id)]
    while heap:
        d, u = heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        for idx_e in graph.adjacency.get(u, ()):
            e = graph.edges[idx_e]
            v = e.to_id
            if v in visited:
                continue
            # Use base cost — we only care about reachability, not cost
            nd = d + e.cost_base
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                heappush(heap, (nd, v))
    return visited


# ---------------------------------------------------------------------------
# Whole-graph connectivity (the strict "every node reaches every other")
# ---------------------------------------------------------------------------


def test_one_giant_strongly_connected_component(graph):
    """Tarjan SCC analysis: every node outside the known freeride pockets
    must live in the single giant SCC. If you see this fail, a new
    pocket has appeared — either a graph-builder regression or a
    legitimately new off-piste feature."""
    sccs = _tarjan_sccs(graph)
    largest = set(sccs[0])
    expected_size = sum(
        1 for n in graph.nodes.values()
        if n.name not in KNOWN_POCKET_NODE_NAMES
    )
    actual_size = sum(
        1 for nid in largest
        if graph.nodes[nid].name not in KNOWN_POCKET_NODE_NAMES
    )
    # Any non-pocket node missing from the giant SCC is a NEW orphan.
    new_orphans = [
        graph.nodes[nid]
        for nid in graph.nodes
        if nid not in largest
        and graph.nodes[nid].name not in KNOWN_POCKET_NODE_NAMES
    ]
    if new_orphans:
        sample = "\n".join(
            f"  - node {n.id} '{n.name}' @ {n.elev:.0f}m" for n in new_orphans[:20]
        )
        pytest.fail(
            f"Tarjan found {len(sccs)} SCCs. Giant SCC contains {len(largest)} / "
            f"{len(graph.nodes)} nodes. {len(new_orphans)} unexpected node(s) "
            f"are outside it:\n{sample}\n\n"
            f"Either fix the graph (add a skating edge to bring them in) or, "
            f"if intentionally off-piste, add the node name(s) to "
            f"KNOWN_POCKET_NODE_NAMES."
        )
    # Belt-and-braces: assert the giant SCC dominates the graph
    assert len(largest) / len(graph.nodes) >= 0.95, (
        f"Giant SCC is only {len(largest)}/{len(graph.nodes)} "
        f"({100 * len(largest) / len(graph.nodes):.1f}%) — connectivity has collapsed"
    )


def test_all_pairs_reachability(graph):
    """N×N reachability matrix. For every ordered pair (u, v) of nodes
    where neither is in a known pocket, v must be reachable from u.

    Cheap: one forward Dijkstra per source fills an entire row of the
    matrix, so we do |V| Dijkstras (≈ 1 s for a 373-node graph)."""
    pocket_node_ids = {
        nid for nid, n in graph.nodes.items()
        if n.name in KNOWN_POCKET_NODE_NAMES
    }
    non_pocket_ids = set(graph.nodes) - pocket_node_ids

    unreachable_pairs: list[tuple[int, int]] = []
    for u in non_pocket_ids:
        reached = _dijkstra_reachable_set(graph, u)
        missing = non_pocket_ids - reached
        for v in missing:
            unreachable_pairs.append((u, v))

    if unreachable_pairs:
        sample = "\n".join(
            f"  - {u} '{graph.nodes[u].name}' → "
            f"{v} '{graph.nodes[v].name}'"
            for u, v in unreachable_pairs[:15]
        )
        more = f"\n  … and {len(unreachable_pairs) - 15} more" if len(unreachable_pairs) > 15 else ""
        pytest.fail(
            f"N×N reachability: {len(unreachable_pairs)} ordered pairs unreachable "
            f"(non-pocket nodes only):\n{sample}{more}"
        )


# ---------------------------------------------------------------------------
# Named-feature reachability (the original L0)
# ---------------------------------------------------------------------------


def test_every_named_feature_has_some_village_reachable(graph, origins):
    """Every named piste/lift top must reach at least one village.

    Snapshot-style: fails when an *unexpected* feature becomes orphaned.
    Pre-known freeride/off-piste features are listed in
    ``KNOWN_ORPHAN_RUNS`` and don't trip the test."""
    orphans: list[Origin] = []
    for o in origins:
        r = skiroute.route_home(graph, o.node_id, mode="any-piste")
        if r is None:
            orphans.append(o)

    unexpected = [o for o in orphans if o.feature_name not in KNOWN_ORPHAN_RUNS]
    if unexpected:
        sample = "\n".join(
            f"  - {o.kind} '{o.feature_name}' (node {o.node_id})" for o in unexpected[:20]
        )
        pytest.fail(
            f"{len(unexpected)} NEW orphan features cannot reach any village "
            f"in any-piste mode (known orphans excluded):\n{sample}\n\n"
            f"If these are off-piste/freeride and intentionally orphaned, "
            f"add them to KNOWN_ORPHAN_RUNS. Otherwise, fix the graph."
        )


# ---------------------------------------------------------------------------
# L1 — routing matrix
# ---------------------------------------------------------------------------


def _filter_known_orphans(
    failures: list[tuple[Origin, str]],
) -> list[tuple[Origin, str]]:
    return [(o, v) for o, v in failures if o.feature_name not in KNOWN_ORPHAN_RUNS]


def test_any_piste_matrix_fully_reachable(graph, origins, village_list):
    """Every non-orphan origin should reach every village in any-piste mode."""
    failures: list[tuple[Origin, str]] = []
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is None:
                failures.append((o, v))

    unexpected = _filter_known_orphans(failures)
    if unexpected:
        sample = "\n".join(
            f"  - {o.kind} '{o.feature_name}' → {v}" for o, v in unexpected[:25]
        )
        pytest.fail(
            f"{len(unexpected)} unexpected OD pairs unreachable in any-piste mode:\n{sample}"
        )


def test_reds_if_needed_matrix_fully_reachable(graph, origins, village_list):
    """reds-if-needed should still cover everything any-piste covers."""
    failures: list[tuple[Origin, str]] = []
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="reds-if-needed")
            if r is None:
                failures.append((o, v))
    unexpected = _filter_known_orphans(failures)
    if unexpected:
        sample = "\n".join(
            f"  - {o.kind} '{o.feature_name}' → {v}" for o, v in unexpected[:25]
        )
        pytest.fail(
            f"{len(unexpected)} unexpected OD pairs unreachable in reds-if-needed mode:\n{sample}"
        )


def test_prefer_easy_matrix_mostly_reachable(graph, origins, village_list):
    """prefer-easy may fail on hard origins, but only up to a small fraction."""
    failures: list[tuple[Origin, str]] = []
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="prefer-easy")
            if r is None:
                failures.append((o, v))
    total = len(origins) * len(village_list)
    frac = len(failures) / total if total else 0.0
    assert frac <= MAX_PREFER_EASY_FAILURE_FRACTION, (
        f"prefer-easy unreachable fraction = {frac:.2%} "
        f"({len(failures)}/{total}) — exceeds threshold "
        f"{MAX_PREFER_EASY_FAILURE_FRACTION:.0%}.\n"
        + "\n".join(f"  - {o.feature_name} → {v}" for o, v in failures[:10])
    )


# ---------------------------------------------------------------------------
# Edge-coverage tally — informational but assertion-backed
# ---------------------------------------------------------------------------


def test_at_least_half_of_runs_are_used_by_router(graph, origins, village_list):
    """Across the full any-piste matrix, the router should exercise a healthy
    fraction of named runs. A very low number means many pistes are graph-
    visible but never selected — usually a connection or cost-model bug."""
    used_runs: set[str] = set()
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is None:
                continue
            for leg in r.legs:
                if leg.type in ("run", "connection") and leg.name:
                    used_runs.add(leg.name)

    all_run_names = {e.name for e in graph.edges
                     if e.type in ("run", "connection") and e.name}
    coverage = len(used_runs) / len(all_run_names) if all_run_names else 0
    assert coverage >= 0.50, (
        f"Only {coverage:.0%} of named runs ({len(used_runs)}/{len(all_run_names)}) "
        f"appear in any returned any-piste route. Inspect coverage_report.html "
        f"for the cold list."
    )


def test_lift_coverage_above_floor(graph, origins, village_list):
    """Sanity floor: a healthy graph should exercise *some* fraction of its
    lifts under any-piste routing. The exact number depends on how dense
    skating edges are — when skate shortcuts exist, the router prefers
    them to riding short lifts. We just want to catch the case where
    lift use collapses entirely."""
    used_lifts: set[str] = set()
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is None:
                continue
            for leg in r.legs:
                if leg.type == "lift" and leg.name:
                    used_lifts.add(leg.name)

    all_lift_names = {e.name for e in graph.edges if e.type == "lift" and e.name}
    coverage = len(used_lifts) / len(all_lift_names) if all_lift_names else 0
    assert coverage >= 0.35, (
        f"Only {coverage:.0%} of named lifts ({len(used_lifts)}/{len(all_lift_names)}) "
        f"appear in any returned any-piste route — drop below 35% suggests "
        f"a cost-model regression or a graph with broken lift connectivity."
    )


# ---------------------------------------------------------------------------
# Mode and objective contracts (sampled — keeps runtime under control)
# ---------------------------------------------------------------------------


def _sample_origins(origins: list[Origin], n: int) -> list[Origin]:
    """Deterministic stride sample so failures are reproducible."""
    if len(origins) <= n:
        return origins
    stride = len(origins) / n
    return [origins[int(i * stride)] for i in range(n)]


def test_prefer_easy_never_faster_than_any_piste(graph, origins, village_list):
    """prefer-easy adds penalties → it can only ever be equal-or-longer."""
    sample = _sample_origins(origins, CONTRACT_SAMPLE_SIZE)
    violations: list[str] = []
    for o in sample:
        for v in village_list:
            easy = skiroute.route_home(graph, o.node_id, [v], mode="prefer-easy")
            anyp = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if easy is None or anyp is None:
                continue
            # Compare real-time costs, not the difficulty-padded ones —
            # easy mode pads costs to *avoid* hard pistes, but the
            # underlying ride time it picks should still be plausible.
            # The skiroute `total_seconds` IS the padded objective value,
            # so easy-padded >= any-padded is the contract.
            if easy.total_seconds < anyp.total_seconds - 1:
                violations.append(
                    f"{o.feature_name} → {v}: easy={easy.total_seconds:.0f}s "
                    f"< any={anyp.total_seconds:.0f}s"
                )
    assert not violations, "prefer-easy returned a faster route than any-piste:\n" + \
        "\n".join(f"  - {v}" for v in violations[:10])


def test_most_skiing_descends_approximately_as_much_as_fastest(graph, origins, village_list):
    """The most-skiing objective should give roughly ≥ vertical vs fastest.

    Strict ≥ doesn't hold because of the post-Dijkstra
    ``_extend_through_destination`` step: different objectives can pick
    different *entry* nodes into the village, and the per-entry tail
    descent differs. We allow up to 7% loss as an empirical bound that
    still catches a real cost-model regression. The threshold has been
    bumped twice as the graph evolved — keep an eye on it: a *sudden*
    jump (e.g. one OD pair going 20% short) is the warning signal."""
    sample = _sample_origins(origins, CONTRACT_SAMPLE_SIZE)
    violations: list[str] = []
    for o in sample:
        for v in village_list:
            fast = skiroute.route_home(graph, o.node_id, [v],
                                   mode="any-piste", objective="fastest")
            most = skiroute.route_home(graph, o.node_id, [v],
                                   mode="any-piste", objective="most-skiing")
            if fast is None or most is None or fast.total_descent_m == 0:
                continue
            shortfall_frac = (fast.total_descent_m - most.total_descent_m) / fast.total_descent_m
            if shortfall_frac > 0.07:
                violations.append(
                    f"{o.feature_name} → {v}: most={most.total_descent_m:.0f}m "
                    f"< fast={fast.total_descent_m:.0f}m ({shortfall_frac:.0%} short)"
                )
    assert not violations, "most-skiing returned >7% less vertical than fastest:\n" + \
        "\n".join(f"  - {v}" for v in violations[:10])


# ---------------------------------------------------------------------------
# Structural invariants — cheap to check, catch a class of router bugs
# ---------------------------------------------------------------------------


def test_returned_routes_are_contiguous(graph, origins, village_list):
    """Consecutive legs must share a node — no teleportation."""
    sample = _sample_origins(origins, CONTRACT_SAMPLE_SIZE)
    bad: list[str] = []
    for o in sample:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is None:
                continue
            for a, b in zip(r.legs, r.legs[1:]):
                if a.to_node.id != b.from_node.id:
                    bad.append(
                        f"{o.feature_name} → {v}: '{a.name}' ends @ {a.to_node.id} "
                        f"but next leg '{b.name}' starts @ {b.from_node.id}"
                    )
                    break
    assert not bad, "Discontinuous routes:\n" + "\n".join(f"  - {x}" for x in bad[:10])


def test_routes_dont_revisit_nodes(graph, origins, village_list):
    """A correct Dijkstra path never visits the same node twice."""
    sample = _sample_origins(origins, CONTRACT_SAMPLE_SIZE)
    bad: list[str] = []
    for o in sample:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is None:
                continue
            visited: set[int] = {r.start_node.id}
            for leg in r.legs:
                for edge in leg.edges:
                    if edge.to_id in visited:
                        bad.append(f"{o.feature_name} → {v}: revisits node {edge.to_id}")
                        break
                    visited.add(edge.to_id)
                else:
                    continue
                break
    assert not bad, "Routes revisit nodes:\n" + "\n".join(f"  - {x}" for x in bad[:10])


# ---------------------------------------------------------------------------
# Aggregate sanity — surfaces graph-builder issues
# ---------------------------------------------------------------------------


def test_every_village_is_reachable_from_somewhere(graph, origins, village_list):
    """Each village should be the chosen endpoint of at least one route.
    A village that never wins is probably tagged on zero nodes."""
    wins: dict[str, int] = defaultdict(int)
    for o in origins:
        for v in village_list:
            r = skiroute.route_home(graph, o.node_id, [v], mode="any-piste")
            if r is not None:
                wins[v] += 1
    missing = [v for v in village_list if wins[v] == 0]
    assert not missing, f"Villages with zero successful inbound routes: {missing}"
