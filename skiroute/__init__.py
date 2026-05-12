"""skiroute — on-piste routing for ski resorts.

Build a routable graph from OpenSkiData and find the fastest way down.

Quickstart:

    import skiroute

    graph = skiroute.build_graph(
        gpkg_path="openskidata.gpkg",
        resort=skiroute.ResortFilter(localities=["Tignes", "Val d'Isère"]),
    )

    result = skiroute.route(
        graph,
        start=(6.9000, 45.4510),
        end=(6.9810, 45.4490),
        mode="prefer-easy",
    )
    print(f"{result.total_seconds/60:.0f} min, {len(result.legs)} legs")
"""

from skiroute import algorithms, contraction_hierarchies
from skiroute.config import (
    BuildConfig,
    Destination,
    DifficultyMode,
    ResortFilter,
    RouteConfig,
    RouteObjective,
)
from skiroute.graph import Edge, Graph, Node
from skiroute.builder import build_graph
from skiroute.router import Leg, Route, route, route_home

__all__ = [
    "BuildConfig",
    "Destination",
    "DifficultyMode",
    "Edge",
    "Graph",
    "Leg",
    "Node",
    "ResortFilter",
    "Route",
    "RouteConfig",
    "RouteObjective",
    "algorithms",
    "contraction_hierarchies",
    "build_graph",
    "route",
    "route_home",
]

__version__ = "0.1.0"
