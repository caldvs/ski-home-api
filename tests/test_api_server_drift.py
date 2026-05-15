"""Drift detector for `examples/api_server.py`.

The example FastAPI server still ships its own copy of the routing
penalty tables, the lift-down constants, and a hand-rolled Dijkstra —
predating the public ``skiroute`` library. Because it's deployed on
Render today, we don't rewrite it in-place. But we do want to know the
moment its constants drift from the library, since that would mean the
HTTP API and the library are giving different routes for the same OD
pair under the same mode.

If this test starts failing, either:
  - bring api_server.py into sync with the new library values, or
  - rewrite api_server.py to import from ``skiroute.config.RouteConfig``
    so this whole class of bug disappears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from skiroute.config import RouteConfig


def _load_api_server():
    """Import examples/api_server.py without running the FastAPI lifecycle.

    The module side-effects-instantiates the graph at import time, so we
    only get here when the bundled tignes.json is present.
    """
    path = Path(__file__).resolve().parents[1] / "examples" / "api_server.py"
    if not path.exists():
        pytest.skip(f"No api_server.py at {path}")
    spec = importlib.util.spec_from_file_location("api_server", path)
    mod = importlib.util.module_from_spec(spec)
    # FastAPI + the example's `graph = SkiGraph(GRAPH_PATH)` global both
    # load on import — they need to succeed or the test environment is
    # already broken. Catch ImportError so a missing FastAPI gives a
    # readable skip instead of a hard fail.
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:
        pytest.skip(f"api_server.py import failed: {e}")
    return mod


def test_difficulty_penalty_table_matches_library():
    """The per-mode {difficulty → seconds} table in the example must
    match RouteConfig.difficulty_penalty exactly, or the example will
    serve different routes than ``skiroute.route()``."""
    api = _load_api_server()
    lib = RouteConfig()
    for mode, lib_row in lib.difficulty_penalty.items():
        assert mode in api.DIFFICULTY_PENALTY, f"api_server is missing mode '{mode}'"
        for diff, lib_val in lib_row.items():
            api_val = api.DIFFICULTY_PENALTY[mode].get(diff)
            assert api_val == lib_val, (
                f"penalty drift: api_server[{mode!r}][{diff!r}]={api_val} "
                f"but RouteConfig.difficulty_penalty[{mode!r}][{diff!r}]={lib_val}"
            )


def test_lift_down_penalty_matches_library():
    """Same drift check for LIFT_DOWN_PENALTY (build-time penalty) and
    the prefer-easy discount factor."""
    api = _load_api_server()
    lib = RouteConfig()
    assert api.LIFT_DOWN_PENALTY == lib.lift_down_penalty, (
        f"LIFT_DOWN_PENALTY drift: api_server={api.LIFT_DOWN_PENALTY}, "
        f"library={lib.lift_down_penalty}"
    )
    # api_server hard-codes the discount inline (line 219: `cost -= LIFT_DOWN_PENALTY * 0.7`).
    # Make sure the library's discount factor is still 0.7 so the
    # in-line literal matches.
    assert lib.lift_down_easy_discount == 0.7, (
        f"lift_down_easy_discount changed to {lib.lift_down_easy_discount}; "
        f"update the hard-coded literal in examples/api_server.py:219"
    )


def test_most_skiing_bonus_matches_library():
    """Drift check for the most-skiing per-metre bonus."""
    api = _load_api_server()
    lib = RouteConfig()
    assert api.MOST_SKIING_RUN_BONUS_PER_M == lib.most_skiing_bonus_per_m, (
        f"MOST_SKIING_RUN_BONUS_PER_M drift: api_server={api.MOST_SKIING_RUN_BONUS_PER_M}, "
        f"library={lib.most_skiing_bonus_per_m}"
    )
