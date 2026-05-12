"""Contraction Hierarchies bench — Tignes/Val d'Isère.

Builds the Tignes graph, preprocesses it into a CHGraph, and compares
CH queries against plain Dijkstra (and A* / bidirectional) on a battery
of random queries. Reports:

  * Preprocessing time + shortcut count
  * Query times: Dijkstra vs CH
  * Correctness check: do CH and Dijkstra agree on path cost?
"""

import os
import random
import time
from pathlib import Path

import skiroute
from skiroute import algorithms as alg
from skiroute import contraction_hierarchies as ch
from skiroute.router import _make_cost_fn
from skiroute.config import RouteConfig


# Set SKIROUTE_GPKG to your OpenSkiData GeoPackage path, or drop it next to
# this script.  Download from https://openskidata.org/.
GPKG = Path(os.environ.get("SKIROUTE_GPKG", "openskidata.gpkg"))
TIGNES_VAL_DESTINATIONS = [
    skiroute.Destination("Tignes Val Claret",   lat=45.4510, lon=6.9000, radius_m=450, elev=2100),
    skiroute.Destination("Tignes Le Lac",       lat=45.4680, lon=6.9070, radius_m=500, elev=2100),
    skiroute.Destination("Tignes Les Boisses",  lat=45.4975, lon=6.9230, radius_m=500, elev=1800),
    skiroute.Destination("Tignes Les Brevieres", lat=45.5080, lon=6.9210, radius_m=500, elev=1550),
    skiroute.Destination("Val d'Isere La Daille", lat=45.4608, lon=6.9638, radius_m=500, elev=1800),
    skiroute.Destination("Val d'Isere Centre",  lat=45.4490, lon=6.9810, radius_m=500, elev=1850),
    skiroute.Destination("Val d'Isere Le Fornet", lat=45.4500, lon=7.0110, radius_m=500, elev=1930),
]


def bench_graph(graph: skiroute.Graph, num_queries: int = 100, label: str = "graph") -> None:
    print(f"\nGraph: {graph}")

    cost_fn = _make_cost_fn("any-piste", "fastest", RouteConfig())

    print("\nPreprocessing CH...")
    ch_graph = ch.preprocess(graph, cost_fn, verbose=True)

    # Generate query pairs
    rng = random.Random(0)
    nodes = list(graph.nodes)
    pairs: list[tuple[int, int]] = []
    while len(pairs) < num_queries:
        s = rng.choice(nodes)
        t = rng.choice(nodes)
        if s != t:
            pairs.append((s, t))

    # --- Correctness check: do CH and Dijkstra agree on path cost? ---
    print("\nCorrectness check (CH vs Dijkstra on 20 queries)...")
    n_mismatched = 0
    for s, t in pairs[:20]:
        d_path, _ = alg.dijkstra(graph, s, t, cost_fn)
        ch_path, _ = ch.query(ch_graph, s, t)
        d_cost = sum(cost_fn(e) for e in d_path) if d_path else None
        ch_cost = sum(cost_fn(e) for e in ch_path) if ch_path else None
        if d_path is None and ch_path is None:
            continue
        if d_path is None or ch_path is None:
            print(f"  ! mismatch {s}→{t}: Dijkstra={d_cost} CH={ch_cost}")
            n_mismatched += 1
        elif abs(d_cost - ch_cost) > 0.01:
            print(f"  ! cost mismatch {s}→{t}: Dijkstra={d_cost:.2f} CH={ch_cost:.2f}")
            n_mismatched += 1
    if n_mismatched == 0:
        print(f"  ✓ all 20 queries agree on cost")
    else:
        print(f"  ✗ {n_mismatched}/20 queries diverged")

    # --- Timing benchmark ---
    print(f"\nTiming on {num_queries} random queries...")

    # Pre-warm caches (graph.reverse_adjacency() is computed lazily on first call)
    graph.reverse_adjacency()

    def time_runner(fn, name):
        total_t = 0.0
        total_visited = 0
        succ = 0
        for s, t in pairs:
            t0 = time.perf_counter()
            res = fn(s, t)
            total_t += (time.perf_counter() - t0)
            if res[0] is not None:
                succ += 1
                total_visited += res[1].nodes_visited
        return name, total_t * 1000 / num_queries, total_visited / max(succ, 1), succ

    def dijkstra_runner(s, t):
        return alg.dijkstra(graph, s, t, cost_fn)

    def astar_runner(s, t):
        return alg.astar(graph, s, t, cost_fn, alg.geographic_heuristic(graph, t))

    def bidir_runner(s, t):
        return alg.bidirectional_dijkstra(graph, s, t, cost_fn)

    def ch_runner(s, t):
        return ch.query(ch_graph, s, t)

    rows = [
        time_runner(dijkstra_runner, "Dijkstra"),
        time_runner(astar_runner, "A*"),
        time_runner(bidir_runner, "Bidirectional Dijkstra"),
        time_runner(ch_runner, "Contraction Hierarchies"),
    ]

    baseline_time = rows[0][1]
    baseline_visited = rows[0][2]
    print()
    print(f"  {'Algorithm':<26} {'Time':>10} {'Visited':>10}  {'vs Dijkstra'}")
    print(f"  {'-'*26} {'-'*10} {'-'*10}  {'-'*22}")
    for name, ms, vis, succ in rows:
        speed = baseline_time / ms if ms else 0
        vis_ratio = vis / baseline_visited if baseline_visited else 0
        print(f"  {name:<26} {ms:>7.3f}ms {vis:>9.0f}    "
              f"{speed:6.1f}× speed, {vis_ratio:.0%} nodes")

    print()
    print(f"  CH preprocessing: {ch_graph.preprocess_seconds:.2f}s one-time")
    print(f"  CH shortcuts added: {ch_graph.n_shortcuts:,} on top of "
          f"{ch_graph.n_original:,} originals "
          f"({ch_graph.n_shortcuts / max(ch_graph.n_original, 1):.0%} overhead)")


def main() -> None:
    print("=" * 72)
    print("CONTRACTION HIERARCHIES BENCH — Tignes / Val d'Isère")
    print("=" * 72)
    if not GPKG.exists():
        print(f"\nGeoPackage not found at {GPKG}. Set SKIROUTE_GPKG=/path/to/openskidata.gpkg")
        print("or drop the file next to this script. Download:")
        print("  https://openskidata.org/")
        return
    print("\nBuilding Tignes graph...")
    tignes = skiroute.build_graph(
        gpkg_path=GPKG,
        resort=skiroute.ResortFilter(ski_area_pattern="%Tignes - Val%"),
        destinations=TIGNES_VAL_DESTINATIONS,
        verbose=False,
    )
    bench_graph(tignes, num_queries=100, label="Tignes")

    savoie_path = Path(os.environ.get("SKIROUTE_SAVOIE", "savoie_mega.json"))
    if savoie_path.exists():
        print()
        print("=" * 72)
        print("CONTRACTION HIERARCHIES BENCH — Savoie 16-resort mega-network")
        print("=" * 72)
        print(f"\nLoading {savoie_path}...")
        savoie = skiroute.Graph.load(savoie_path)
        bench_graph(savoie, num_queries=100, label="Savoie")
    else:
        print(f"\nSkipping Savoie bench — {savoie_path} not found.")
        print("Run savoie_mega.py first to generate it.")


if __name__ == "__main__":
    main()
