"""Detect graph drift between the API/library and the front-end.

The CLAUDE.md data flow is::

    OpenSkiData GPKG → ski-home/build_graph.py → graph.json
                                                  ↓ copied to
                                            ski-home-api/examples/data/tignes.json
                                            ski-home-map/data/tignes.json

If the two copies drift, the UI plots a route the API would not, and
vice-versa. This test makes the drift loud, so re-syncing is a
conscious decision rather than a silent inconsistency.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
API_GRAPH = REPO_ROOT / "ski-home-api" / "examples" / "data" / "tignes.json"
MAP_GRAPH = REPO_ROOT / "ski-home-map" / "data" / "tignes.json"


def _summary(path: Path) -> dict:
    data = json.loads(path.read_text())
    return {
        "nodes": len(data["nodes"]),
        "edges": len(data["edges"]),
        "villages": sorted(data.get("villages", {}).keys()),
        "edge_keys": frozenset(
            (e["from"], e["to"], e["type"], e.get("name", ""))
            for e in data["edges"]
        ),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
    }


@pytest.mark.skipif(
    not MAP_GRAPH.exists() or not API_GRAPH.exists(),
    reason="One of the graph copies is missing",
)
def test_api_and_map_graphs_match():
    api = _summary(API_GRAPH)
    mp = _summary(MAP_GRAPH)

    if api["edge_keys"] == mp["edge_keys"] and api["nodes"] == mp["nodes"]:
        return  # in sync

    only_api = api["edge_keys"] - mp["edge_keys"]
    only_map = mp["edge_keys"] - api["edge_keys"]
    pytest.fail(
        "API and map graphs have drifted — re-run `python ski-home/build_graph.py` "
        "and copy graph.json into both ski-home-api/examples/data/ and "
        "ski-home-map/data/.\n"
        f"  api : {api['nodes']} nodes, {api['edges']} edges, sha {api['sha256']}\n"
        f"  map : {mp['nodes']} nodes, {mp['edges']} edges, sha {mp['sha256']}\n"
        f"  edges in api only: {len(only_api)}\n"
        f"  edges in map only: {len(only_map)}\n"
        f"  village sets equal: {api['villages'] == mp['villages']}"
    )
