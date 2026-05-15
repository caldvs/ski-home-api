"""Unit tests for the new override operations the piste editor emits.

Builds a tiny synthetic graph (so we don't depend on any GPKG) and
exercises each of the three new override types:

  - ``remove_named_features``  drops every edge with that name and
                               any nodes that become unincident.
  - ``rename_features``        renames the feature across all edges
                               and rewrites matching node names.
  - ``set_difficulty``         overwrites the difficulty on run/
                               connection edges of that name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import skiroute
from skiroute.graph import Edge, Graph, Node
from skiroute.overrides import apply_overrides


def _toy_graph() -> Graph:
    """Five-node toy graph with two named runs and a lift.

        A.top --run "Alpha"--> A.bot --skate--> B.top --run "Beta"--> B.bot
        Lift "L1" from B.bot to A.top (so it's strongly connected)
        Plus an orphaned named run "Zeta" from Z.top to Z.bot (no other edges)
    """
    g = Graph()
    g.nodes = {
        1: Node(id=1, lon=0.0, lat=0.0, elev=2500, name="Alpha top"),
        2: Node(id=2, lon=0.0, lat=0.0, elev=2000, name="Alpha bottom"),
        3: Node(id=3, lon=0.0, lat=0.0, elev=2000, name="Beta top"),
        4: Node(id=4, lon=0.0, lat=0.0, elev=1500, name="Beta bottom"),
        5: Node(id=5, lon=0.1, lat=0.1, elev=2800, name="Zeta top"),
        6: Node(id=6, lon=0.1, lat=0.1, elev=2300, name="Zeta bottom"),
    }
    g.edges = [
        Edge(from_id=1, to_id=2, type="run", name="Alpha",
             cost_base=100, length_m=500, difficulty="easy"),
        Edge(from_id=2, to_id=3, type="skate", name="",
             cost_base=30, length_m=50),
        Edge(from_id=3, to_id=4, type="run", name="Beta",
             cost_base=120, length_m=600, difficulty="advanced"),
        Edge(from_id=4, to_id=1, type="lift", name="L1",
             cost_base=300, length_m=2000, lift_type="chair_lift"),
        Edge(from_id=5, to_id=6, type="run", name="Zeta",
             cost_base=80, length_m=400, difficulty="freeride"),
    ]
    for idx, e in enumerate(g.edges):
        g.adjacency.setdefault(e.from_id, []).append(idx)
    return g


def _write_overrides(tmp_path: Path, body: dict[str, Any]) -> Path:
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(body))
    return path


# ---------------------------------------------------------------------------
# remove_named_features
# ---------------------------------------------------------------------------


def test_remove_named_feature_drops_edges_and_isolated_nodes(tmp_path):
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "remove_named_features": [{"name": "Zeta"}]
    })

    assert any(e.name == "Zeta" for e in g.edges)
    assert 5 in g.nodes and 6 in g.nodes

    apply_overrides(g, overrides, verbose=False)

    # The Zeta edge is gone.
    assert not any(e.name == "Zeta" for e in g.edges)
    # Zeta's only-incident nodes are gone too.
    assert 5 not in g.nodes
    assert 6 not in g.nodes
    # The rest of the graph is untouched.
    assert any(e.name == "Alpha" for e in g.edges)
    assert any(e.name == "Beta" for e in g.edges)
    assert 1 in g.nodes


def test_remove_named_feature_preserves_shared_nodes(tmp_path):
    """When a removed feature shares a node with a kept feature, the node stays."""
    g = _toy_graph()
    # Add a second run that shares Beta's top node so it survives Beta removal.
    g.nodes[7] = Node(id=7, lon=0.0, lat=0.0, elev=1500, name="Gamma bottom")
    extra = Edge(from_id=3, to_id=7, type="run", name="Gamma",
                 cost_base=100, length_m=400, difficulty="easy")
    g.edges.append(extra)
    g.adjacency.setdefault(3, []).append(len(g.edges) - 1)

    overrides = _write_overrides(tmp_path, {
        "remove_named_features": [{"name": "Beta"}]
    })
    apply_overrides(g, overrides, verbose=False)

    assert not any(e.name == "Beta" for e in g.edges)
    # Beta-top (node 3) still in graph because Gamma uses it.
    assert 3 in g.nodes
    # Beta-bottom (node 4) WAS only used by Beta and the L1 lift returning to
    # Alpha-top — so it's still incident to L1.
    assert 4 in g.nodes


def test_remove_named_feature_rebuilds_adjacency(tmp_path):
    """Edge indices change after removal — adjacency must be consistent."""
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "remove_named_features": [{"name": "Zeta"}]
    })
    apply_overrides(g, overrides, verbose=False)

    # Every adjacency entry must point at a valid edge whose from_id matches.
    for nid, idxs in g.adjacency.items():
        for idx in idxs:
            assert 0 <= idx < len(g.edges)
            assert g.edges[idx].from_id == nid


def test_remove_named_feature_warns_on_no_match(tmp_path, capsys):
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "remove_named_features": [{"name": "DoesNotExist"}]
    })
    apply_overrides(g, overrides, verbose=True)
    captured = capsys.readouterr()
    assert "matched no edges" in captured.out


# ---------------------------------------------------------------------------
# rename_features
# ---------------------------------------------------------------------------


def test_rename_feature_renames_edges_and_nodes(tmp_path):
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "rename_features": [{"old_name": "Alpha", "new_name": "Aurora"}]
    })

    apply_overrides(g, overrides, verbose=False)

    # All Alpha edges are now Aurora
    assert not any(e.name == "Alpha" for e in g.edges)
    assert any(e.name == "Aurora" for e in g.edges)
    # Node names like "Alpha top" → "Aurora top"
    assert g.nodes[1].name == "Aurora top"
    assert g.nodes[2].name == "Aurora bottom"
    # Other features untouched
    assert g.nodes[3].name == "Beta top"


def test_rename_does_not_rename_unrelated_substring_nodes(tmp_path):
    """Renaming 'Alpha' must not touch a node called 'Alphabet top'."""
    g = _toy_graph()
    g.nodes[8] = Node(id=8, lon=0.0, lat=0.0, elev=2500, name="Alphabet top")
    overrides = _write_overrides(tmp_path, {
        "rename_features": [{"old_name": "Alpha", "new_name": "Aurora"}]
    })
    apply_overrides(g, overrides, verbose=False)
    assert g.nodes[8].name == "Alphabet top", "Substring match should be ignored"


# ---------------------------------------------------------------------------
# set_difficulty
# ---------------------------------------------------------------------------


def test_set_difficulty_overwrites_run_edges(tmp_path):
    g = _toy_graph()
    # Beta starts as advanced
    beta_before = [e for e in g.edges if e.name == "Beta"][0]
    assert beta_before.difficulty == "advanced"

    overrides = _write_overrides(tmp_path, {
        "set_difficulty": [{"name": "Beta", "difficulty": "intermediate"}]
    })
    apply_overrides(g, overrides, verbose=False)

    beta_after = [e for e in g.edges if e.name == "Beta"][0]
    assert beta_after.difficulty == "intermediate"
    # Other named features are not retyped
    alpha = [e for e in g.edges if e.name == "Alpha"][0]
    assert alpha.difficulty == "easy"


def test_set_difficulty_skips_non_run_edges(tmp_path):
    """A 'set_difficulty' on a lift name has no effect — only run/connection edges are retyped."""
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "set_difficulty": [{"name": "L1", "difficulty": "easy"}]
    })
    apply_overrides(g, overrides, verbose=False)

    l1 = [e for e in g.edges if e.name == "L1"][0]
    assert l1.difficulty == "", "Lift edge should not gain a difficulty"


# ---------------------------------------------------------------------------
# remove_edges (per-segment)
# ---------------------------------------------------------------------------


def test_remove_edges_drops_only_the_specified_edge(tmp_path):
    """Remove a single segment by (from_name, to_name) and verify the rest
    of the named feature survives."""
    g = _toy_graph()
    # Add a second 'Beta' edge (segment) so we can prove only one is removed.
    g.nodes[8] = Node(id=8, lon=0.0, lat=0.0, elev=1700, name="Beta middle")
    seg2 = Edge(from_id=3, to_id=8, type="run", name="Beta",
                cost_base=70, length_m=300, difficulty="advanced")
    g.edges.append(seg2)
    g.adjacency.setdefault(3, []).append(len(g.edges) - 1)

    overrides = _write_overrides(tmp_path, {
        "remove_edges": [
            {"from_name": "Beta top", "to_name": "Beta middle", "type": "run"}
        ]
    })
    apply_overrides(g, overrides, verbose=False)

    # The Beta-top → Beta-middle edge is gone
    assert not any(
        e.from_id == 3 and e.to_id == 8 for e in g.edges
    )
    # The other Beta edge (top → bottom) is still here
    assert any(
        e.name == "Beta" and e.from_id == 3 and e.to_id == 4 for e in g.edges
    )
    # The orphaned 'Beta middle' node was dropped
    assert 8 not in g.nodes


def test_remove_edges_warns_on_no_match(tmp_path, capsys):
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "remove_edges": [{"from_name": "Nope", "to_name": "Nothing", "type": "run"}]
    })
    apply_overrides(g, overrides, verbose=True)
    assert "did not resolve" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# from_id / to_id disambiguation (name collisions)
# ---------------------------------------------------------------------------


def test_add_edges_accepts_from_id_to_id(tmp_path):
    """When two nodes share a name, names are ambiguous. The override
    schema must accept node IDs as an unambiguous alternative."""
    g = _toy_graph()
    # Build a name collision: two distinct nodes both called "DupName"
    g.nodes[100] = Node(id=100, lon=0.0, lat=0.0, elev=2000, name="DupName")
    g.nodes[101] = Node(id=101, lon=0.1, lat=0.1, elev=1900, name="DupName")
    # Add a route edge so node 100 is in the graph adjacency map
    g.edges.append(Edge(from_id=100, to_id=101, type="run", name="Helper",
                       cost_base=10, length_m=10, difficulty="easy"))
    g.adjacency.setdefault(100, []).append(len(g.edges) - 1)

    # Without IDs, "DupName" → "DupName" would pick whichever node is
    # iteration-first — wrong half the time. With explicit IDs, both
    # endpoints are unambiguous.
    overrides = _write_overrides(tmp_path, {
        "add_edges": [
            {"from_id": 101, "to_id": 100, "type": "skate",
             "name": "Custom skate"}
        ]
    })
    apply_overrides(g, overrides, verbose=False)

    matches = [e for e in g.edges if e.name == "Custom skate"]
    assert len(matches) == 1
    assert matches[0].from_id == 101 and matches[0].to_id == 100


def test_remove_edges_accepts_from_id_to_id(tmp_path):
    g = _toy_graph()
    # Identify the edge to remove by ID (the Beta run from node 3 → 4)
    overrides = _write_overrides(tmp_path, {
        "remove_edges": [{"from_id": 3, "to_id": 4, "type": "run"}]
    })
    apply_overrides(g, overrides, verbose=False)

    assert not any(e.from_id == 3 and e.to_id == 4 for e in g.edges)
    # The other Beta-named edges (we have none in toy graph by default)
    # would survive — checked indirectly: alpha edge still present.
    assert any(e.name == "Alpha" for e in g.edges)


def test_resolve_endpoint_returns_none_for_missing_id(tmp_path, capsys):
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "add_edges": [{"from_id": 9999, "to_id": 1, "type": "skate"}]
    })
    apply_overrides(g, overrides, verbose=True)
    out = capsys.readouterr().out
    assert "id=9999" in out and "not found" in out


def test_name_collision_warns_with_id_hint(tmp_path, capsys):
    """When `_name` matches >1 node the user can't tell which got picked.
    The override must print a warning naming the candidates and telling
    them to use `_id` instead."""
    g = _toy_graph()
    # Two nodes both called "Twin"
    g.nodes[200] = Node(id=200, lon=0.0, lat=0.0, elev=2500, name="Twin")
    g.nodes[201] = Node(id=201, lon=0.1, lat=0.1, elev=2400, name="Twin")
    # Add an outgoing edge from node 200 so add_edges doesn't bail on
    # "no incident edges".
    g.edges.append(Edge(from_id=200, to_id=1, type="skate", name="seed",
                       cost_base=10, length_m=10))
    g.adjacency.setdefault(200, []).append(len(g.edges) - 1)

    overrides = _write_overrides(tmp_path, {
        "add_edges": [{"from_name": "Twin", "to_name": "Alpha top",
                       "type": "skate"}]
    })
    apply_overrides(g, overrides, verbose=True)
    out = capsys.readouterr().out
    assert "matched 2 nodes" in out
    assert "from_id=" in out, "warning should hint at the from_id alternative"


# ---------------------------------------------------------------------------
# add_edges preserves every edge field the schema accepts
# ---------------------------------------------------------------------------


def test_add_edges_preserves_lift_type_and_elev_drop(tmp_path):
    """Regression: lift_type and elev_drop were dropped by an earlier
    version of apply_overrides. A magic-carpet lift entered as an
    override must surface with type=lift AND lift_type=magic_carpet."""
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "add_edges": [
            {
                "from_id": 4, "to_id": 1,  # B.bot → A.top
                "type": "lift",
                "name": "Pim Pam",
                "lift_type": "magic_carpet",
                "cost_override": 240,
            }
        ]
    })
    apply_overrides(g, overrides, verbose=False)

    pim = [e for e in g.edges if e.name == "Pim Pam"]
    assert len(pim) == 1
    assert pim[0].type == "lift"
    assert pim[0].lift_type == "magic_carpet"
    assert pim[0].cost_base == 240


# ---------------------------------------------------------------------------
# Routing still works after removals
# ---------------------------------------------------------------------------


def test_router_works_after_remove(tmp_path):
    """Sanity: after removing Zeta, routing on the remaining graph still works."""
    g = _toy_graph()
    overrides = _write_overrides(tmp_path, {
        "remove_named_features": [{"name": "Zeta"}]
    })
    apply_overrides(g, overrides, verbose=False)

    r = skiroute.route(g, start=1, end=4, mode="any-piste")
    assert r is not None
    assert r.total_seconds > 0
    assert any(leg.name == "Alpha" for leg in r.legs)
    assert any(leg.name == "Beta" for leg in r.legs)
