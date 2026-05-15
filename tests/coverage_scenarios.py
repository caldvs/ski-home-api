"""Shared helpers for resort-coverage testing.

Builds the universe of things we test against:

  - **Named features** — every distinct piste name and lift name appearing
    on at least one edge. These are the units of *coverage* (a piste is
    "covered" if at least one returned route uses it).

  - **Origins** — one representative node per named feature, taken as
    the highest-elevation endpoint of any edge with that name. Skiers
    start at the *top* of a piste or after exiting a lift, so this is
    where realistic routing starts.

  - **Destinations** — the village names already tagged on the graph
    (``graph.destinations``).

The same building blocks are reused by the pytest suite and the
standalone HTML report generator.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import skiroute


GRAPH_PATH = Path(__file__).parent.parent / "examples" / "data" / "tignes.json"


# Difficulty modes and routing objectives. Kept here so the pytest
# tests and the HTML report stay in sync.
MODES: tuple[str, ...] = ("prefer-easy", "reds-if-needed", "any-piste")
OBJECTIVES: tuple[str, ...] = ("fastest", "most-skiing")


@dataclass(frozen=True)
class Origin:
    """A representative starting point: the top of a named piste/lift."""
    node_id: int
    kind: str          # "run" or "lift"
    feature_name: str


def load_graph(path: Path | str | None = None) -> skiroute.Graph:
    return skiroute.Graph.load(Path(path) if path else GRAPH_PATH)


def named_features(graph: skiroute.Graph) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Group edges by name. Thin shim around ``Graph.named_features()``.

    Returns ``(runs, lifts)`` where each maps a feature name to the list
    of edge indices carrying that name.
    """
    groups = graph.named_features()
    runs: dict[str, list[int]] = {}
    lifts: dict[str, list[int]] = {}
    for (ty, name), idxs in groups.items():
        (runs if ty == "run" else lifts)[name] = idxs
    return runs, lifts


def top_node(graph: skiroute.Graph, edge_indices: list[int]) -> int:
    """The highest-elevation node touched by any of these edges.

    For a lift this is the top station; for a run this is the entry
    point at the top of the piste.
    """
    candidates: set[int] = set()
    for idx in edge_indices:
        e = graph.edges[idx]
        candidates.add(e.from_id)
        candidates.add(e.to_id)
    return max(candidates, key=lambda nid: graph.nodes[nid].elev)


def build_origins(graph: skiroute.Graph) -> list[Origin]:
    """One Origin per named piste or lift, anchored at the top node."""
    runs, lifts = named_features(graph)
    origins: list[Origin] = []
    for name, idxs in runs.items():
        origins.append(Origin(top_node(graph, idxs), "run", name))
    for name, idxs in lifts.items():
        origins.append(Origin(top_node(graph, idxs), "lift", name))
    return origins


def villages(graph: skiroute.Graph) -> list[str]:
    return list(graph.destinations.keys())


def feature_endpoints(graph: skiroute.Graph, edge_indices: list[int]) -> tuple[int, int]:
    """Return ``(top_node_id, bottom_node_id)`` for a named feature."""
    candidates: set[int] = set()
    for idx in edge_indices:
        e = graph.edges[idx]
        candidates.add(e.from_id)
        candidates.add(e.to_id)
    elevs = {nid: graph.nodes[nid].elev for nid in candidates}
    top = max(elevs, key=elevs.get)
    bottom = min(elevs, key=elevs.get)
    return top, bottom


