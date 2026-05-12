"""Tignes / Val d'Isère quickstart for skiroute.

Demonstrates the three layers of the public API:

  1. build_graph()  — turn an OpenSkiData GPKG into a routable graph
  2. route()        — point-to-point routing
  3. route_home()   — route to nearest of N destination villages

To run this you need `openskidata.gpkg` in this directory. Download
the latest from https://openskidata.org/ (look for the GeoPackage
release; it covers every ski area in the world).

    python3 tignes_quickstart.py
"""

import os
from pathlib import Path

import skiroute


GPKG = Path(os.environ.get("SKIROUTE_GPKG", "openskidata.gpkg"))

TIGNES_DESTINATIONS = [
    skiroute.Destination("Tignes Val Claret",       lat=45.4510, lon=6.9000, radius_m=450, elev=2100),
    skiroute.Destination("Tignes Le Lac",           lat=45.4680, lon=6.9070, radius_m=500, elev=2100),
    skiroute.Destination("Tignes Les Boisses",      lat=45.4975, lon=6.9230, radius_m=500, elev=1800),
    skiroute.Destination("Tignes Les Brevieres",    lat=45.5080, lon=6.9210, radius_m=500, elev=1550),
    skiroute.Destination("Val d'Isere La Daille",   lat=45.460761, lon=6.96382, radius_m=500, elev=1800),
    skiroute.Destination("Val d'Isere Centre",      lat=45.4490, lon=6.9810, radius_m=500, elev=1850),
    skiroute.Destination("Val d'Isere Le Laisinant", lat=45.4471, lon=6.9943, radius_m=400, elev=1860),
    skiroute.Destination("Val d'Isere Le Fornet",   lat=45.4500, lon=7.0110, radius_m=500, elev=1930),
]


def main() -> None:
    graph = skiroute.build_graph(
        gpkg_path=GPKG,
        resort=skiroute.ResortFilter(localities=["Tignes", "Val d'Isère"]),
        destinations=TIGNES_DESTINATIONS,
    )

    # Save for the iOS app / API (compatible with existing format)
    graph.save("tignes.json")

    # Where am I right now? (somewhere near the top of Solaise)
    start = (6.985, 45.452)

    print("\n-- prefer-easy --")
    r = skiroute.route_home(graph, start, mode="prefer-easy")
    print(f"  → {r.end_node.name}: {r.total_minutes:.0f} min, "
          f"{r.total_descent_m:.0f}m descent, {len(r.legs)} legs")

    print("\n-- most-skiing --")
    r = skiroute.route_home(graph, start, mode="any-piste", objective="most-skiing")
    print(f"  → {r.end_node.name}: {r.total_minutes:.0f} min, "
          f"{r.total_descent_m:.0f}m descent, {len(r.legs)} legs")


if __name__ == "__main__":
    main()
