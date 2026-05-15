"""Run the full resort-coverage matrix and emit an interactive HTML report.

Usage::

    python tests/coverage_report.py                         # writes tests/coverage_report.html
    python tests/coverage_report.py /tmp/out.html

The report shows every named run/lift colour-coded by how many routes
in the test matrix used it:

  - red       — never used (suspicious: graph hole or unreachable feature)
  - amber     — rarely used
  - green     — well-exercised

For each origin (top of a piste or lift) we attempt to route home to
each of the resort's villages under every difficulty mode and both
objectives. Failures are listed in the sidebar.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import skiroute

# Allow `python tests/coverage_report.py` to find coverage_scenarios.
sys.path.insert(0, str(Path(__file__).parent))
from coverage_scenarios import (  # noqa: E402
    GRAPH_PATH,
    MODES,
    OBJECTIVES,
    Origin,
    build_origins,
    load_graph,
    named_features,
    villages,
)


# ---------------------------------------------------------------------------
# Matrix runner
# ---------------------------------------------------------------------------


def run_matrix(
    graph: skiroute.Graph,
    origins: list[Origin],
    village_list: list[str],
    modes: Iterable[str] = MODES,
    objectives: Iterable[str] = OBJECTIVES,
) -> tuple[dict[tuple[str, str], int], list[dict]]:
    """Run every (origin × village × mode × objective) route.

    Returns:
        edge_hits: {(kind, feature_name): count} — how many routes used each feature.
        failures: list of ``{"origin", "village", "mode", "objective"}`` dicts
            for routes that returned None.
    """
    edge_hits: dict[tuple[str, str], int] = defaultdict(int)
    failures: list[dict] = []

    for o in origins:
        for v in village_list:
            for mode in modes:
                for objective in objectives:
                    r = skiroute.route_home(
                        graph, o.node_id, [v], mode=mode, objective=objective
                    )
                    if r is None:
                        failures.append({
                            "kind": o.kind,
                            "feature": o.feature_name,
                            "village": v,
                            "mode": mode,
                            "objective": objective,
                        })
                        continue
                    # Tally each named run/lift used. A feature counts once
                    # per route even if it spans multiple legs.
                    seen: set[tuple[str, str]] = set()
                    for leg in r.legs:
                        if not leg.name:
                            continue
                        if leg.type in ("run", "connection"):
                            seen.add(("run", leg.name))
                        elif leg.type == "lift":
                            seen.add(("lift", leg.name))
                    for key in seen:
                        edge_hits[key] += 1

    return dict(edge_hits), failures


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _feature_class(hits: int, hot_threshold: int) -> str:
    if hits == 0:
        return "cold"
    if hits >= hot_threshold:
        return "hot"
    return "warm"


def _render_feature_list(rows: list[tuple[str, int]], hot_threshold: int) -> str:
    parts = []
    for name, hits in rows:
        cls = _feature_class(hits, hot_threshold)
        # Escape minimally — feature names are clean OSM strings.
        safe = name.replace("<", "&lt;").replace(">", "&gt;")
        parts.append(
            f'<div class="feature {cls}" data-name="{safe}">'
            f'<span class="name">{safe}</span>'
            f'<span class="hits">{hits}</span></div>'
        )
    return "".join(parts)


def render_html(
    graph: skiroute.Graph,
    edge_hits: dict[tuple[str, str], int],
    failures: list[dict],
    runs_by_name: dict[str, list[int]],
    lifts_by_name: dict[str, list[int]],
    total_routes: int,
    elapsed_s: float,
) -> str:
    """Build the standalone HTML report."""
    run_rows = sorted(
        ((name, edge_hits.get(("run", name), 0)) for name in runs_by_name),
        key=lambda r: (r[1], r[0]),
    )
    lift_rows = sorted(
        ((name, edge_hits.get(("lift", name), 0)) for name in lifts_by_name),
        key=lambda r: (r[1], r[0]),
    )

    covered_runs = sum(1 for _, h in run_rows if h > 0)
    covered_lifts = sum(1 for _, h in lift_rows if h > 0)
    uncovered_runs = [(n, h) for n, h in run_rows if h == 0]
    uncovered_lifts = [(n, h) for n, h in lift_rows if h == 0]

    # 95th-percentile hits as "hot" threshold so colour scales sensibly.
    all_hits = sorted([h for _, h in run_rows + lift_rows if h > 0])
    hot_threshold = (
        all_hits[int(len(all_hits) * 0.75)] if all_hits else 1
    )

    # GeoJSON for every named run/lift edge so the map can colour each segment.
    edge_features = []
    for e in graph.edges:
        if e.type not in ("run", "connection", "lift") or not e.name or not e.geometry:
            continue
        kind = "lift" if e.type == "lift" else "run"
        edge_features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[c[0], c[1]] for c in e.geometry],
            },
            "properties": {
                "name": e.name,
                "kind": kind,
                "difficulty": e.difficulty or "",
                "hits": edge_hits.get((kind, e.name), 0),
            },
        })

    failure_rows = "".join(
        f'<tr><td>{f["kind"]}</td><td>{f["feature"]}</td>'
        f'<td>{f["village"]}</td><td>{f["mode"]}</td><td>{f["objective"]}</td></tr>'
        for f in failures[:200]
    )
    if len(failures) > 200:
        failure_rows += f'<tr><td colspan="5" class="muted">… and {len(failures) - 200} more</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Resort coverage report</title>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font: 13px/1.45 -apple-system, system-ui, sans-serif; margin: 0; color: #222; }}
  header {{ padding: 10px 16px; background: #1a1a1a; color: #fff; display: flex; align-items: baseline; gap: 16px; }}
  header h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
  header .meta {{ color: #aaa; font-size: 12px; }}
  .summary {{ display: flex; gap: 16px; padding: 10px 16px; background: #f4f4f4; border-bottom: 1px solid #ddd; flex-wrap: wrap; }}
  .summary .stat {{ font-size: 12px; color: #555; }}
  .summary .stat strong {{ display: block; font-size: 18px; color: #222; font-variant-numeric: tabular-nums; }}
  main {{ display: grid; grid-template-columns: 1fr 380px; height: calc(100vh - 90px); }}
  #map {{ height: 100%; }}
  .side {{ overflow-y: auto; border-left: 1px solid #ddd; }}
  .side section {{ padding: 10px 12px; border-bottom: 1px solid #eee; }}
  .side h3 {{ margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #666; }}
  .feature {{ display: flex; justify-content: space-between; padding: 3px 6px; border-radius: 3px; cursor: pointer; }}
  .feature:hover {{ background: #f0f6ff; }}
  .feature.cold {{ background: #fde7e7; color: #9b1c1c; }}
  .feature.warm {{ background: #fff5d6; color: #8a6800; }}
  .feature.hot {{ background: #e3f3e6; color: #1f6b2f; }}
  .feature .hits {{ font-variant-numeric: tabular-nums; opacity: .7; }}
  .tab-row {{ display: flex; gap: 4px; padding: 6px 12px; background: #fafafa; border-bottom: 1px solid #eee; }}
  .tab-row button {{ background: white; border: 1px solid #ccc; padding: 4px 10px; border-radius: 3px; cursor: pointer; font-size: 12px; }}
  .tab-row button.active {{ background: #1a1a1a; color: white; border-color: #1a1a1a; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
  table.failures {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
  table.failures th, table.failures td {{ text-align: left; padding: 3px 6px; border-bottom: 1px solid #eee; }}
  table.failures th {{ background: #fafafa; font-weight: 600; color: #555; }}
  .muted {{ color: #888; }}
  .legend {{ display: flex; gap: 8px; font-size: 11px; margin-top: 6px; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
  .legend i {{ width: 12px; height: 4px; display: inline-block; }}
</style>
</head>
<body>

<header>
  <h1>Resort coverage report</h1>
  <span class="meta">{total_routes:,} routes · {elapsed_s:.1f}s · {len(failures)} failures</span>
</header>

<div class="summary">
  <div class="stat"><strong>{covered_runs}/{len(run_rows)}</strong>runs covered</div>
  <div class="stat"><strong>{covered_lifts}/{len(lift_rows)}</strong>lifts covered</div>
  <div class="stat"><strong>{total_routes - len(failures):,}/{total_routes:,}</strong>routes succeeded</div>
  <div class="stat"><strong>{len(failures):,}</strong>routes failed</div>
  <div class="stat"><strong>{len(uncovered_runs)}</strong>cold runs</div>
  <div class="stat"><strong>{len(uncovered_lifts)}</strong>cold lifts</div>
</div>

<main>
  <div id="map">
    <div class="legend" style="position:absolute; bottom: 12px; left: 12px; background: white; padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; z-index: 1;">
      <span><i style="background:#e63946"></i> 0 hits</span>
      <span><i style="background:#f4a261"></i> few</span>
      <span><i style="background:#74c69d"></i> many</span>
      <span><i style="background:#1b4332"></i> hot</span>
    </div>
  </div>

  <aside class="side">
    <div class="tab-row">
      <button class="active" data-tab="runs">Runs ({len(run_rows)})</button>
      <button data-tab="lifts">Lifts ({len(lift_rows)})</button>
      <button data-tab="fails">Failures ({len(failures)})</button>
    </div>

    <div class="tab-content active" id="tab-runs">
      <section>
        <h3>Uncovered runs ({len(uncovered_runs)})</h3>
        {_render_feature_list(uncovered_runs, hot_threshold) if uncovered_runs else '<div class="muted">All runs covered ✓</div>'}
      </section>
      <section>
        <h3>All runs (cold → hot)</h3>
        {_render_feature_list(run_rows, hot_threshold)}
      </section>
    </div>

    <div class="tab-content" id="tab-lifts">
      <section>
        <h3>Uncovered lifts ({len(uncovered_lifts)})</h3>
        {_render_feature_list(uncovered_lifts, hot_threshold) if uncovered_lifts else '<div class="muted">All lifts covered ✓</div>'}
      </section>
      <section>
        <h3>All lifts (cold → hot)</h3>
        {_render_feature_list(lift_rows, hot_threshold)}
      </section>
    </div>

    <div class="tab-content" id="tab-fails">
      <section>
        <h3>Failed routes (first 200)</h3>
        <table class="failures">
          <thead><tr><th>kind</th><th>from</th><th>to village</th><th>mode</th><th>objective</th></tr></thead>
          <tbody>{failure_rows or '<tr><td colspan="5" class="muted">No failures ✓</td></tr>'}</tbody>
        </table>
      </section>
    </div>
  </aside>
</main>

<script>
const EDGES = {{ type: "FeatureCollection", features: {json.dumps(edge_features)} }};
const HOT = {hot_threshold};

const map = new maplibregl.Map({{
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/positron',
  center: [6.94, 45.45],
  zoom: 12,
}});

map.on('load', () => {{
  map.addSource('edges', {{ type: 'geojson', data: EDGES }});
  map.addLayer({{
    id: 'edges-line',
    type: 'line',
    source: 'edges',
    paint: {{
      'line-width': [
        'case',
        ['==', ['get', 'kind'], 'lift'], 2,
        3
      ],
      'line-dasharray': [
        'case',
        ['==', ['get', 'kind'], 'lift'], ['literal', [2, 2]],
        ['literal', [1]]
      ],
      'line-color': [
        'case',
        ['==', ['get', 'hits'], 0], '#e63946',
        ['<=', ['get', 'hits'], HOT * 0.25], '#f4a261',
        ['<=', ['get', 'hits'], HOT], '#74c69d',
        '#1b4332'
      ],
      'line-opacity': 0.85
    }}
  }});

  const popup = new maplibregl.Popup({{ closeButton: false, offset: 8 }});
  map.on('mousemove', 'edges-line', (e) => {{
    map.getCanvas().style.cursor = 'pointer';
    const p = e.features[0].properties;
    popup.setLngLat(e.lngLat)
      .setHTML(`<strong>${{p.name}}</strong><br>${{p.kind}}${{p.difficulty ? ' · ' + p.difficulty : ''}}<br>Hits: ${{p.hits}}`)
      .addTo(map);
  }});
  map.on('mouseleave', 'edges-line', () => {{
    map.getCanvas().style.cursor = '';
    popup.remove();
  }});

  // Click a feature in the sidebar to flash it on the map
  document.querySelectorAll('.feature').forEach(el => {{
    el.addEventListener('click', () => {{
      const name = el.dataset.name;
      const matches = EDGES.features.filter(f => f.properties.name === name);
      if (!matches.length) return;
      const coords = matches.flatMap(f => f.geometry.coordinates);
      const lons = coords.map(c => c[0]);
      const lats = coords.map(c => c[1]);
      map.fitBounds([
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)]
      ], {{ padding: 80, maxZoom: 15, duration: 600 }});
    }});
  }});
}});

// Tab switching
document.querySelectorAll('.tab-row button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-row button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  }});
}});
</script>

</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "coverage_report.html"

    print(f"Loading graph from {GRAPH_PATH} …")
    graph = load_graph()
    print(f"  {graph}")

    runs_by_name, lifts_by_name = named_features(graph)
    origins = build_origins(graph)
    village_list = villages(graph)
    total_routes = len(origins) * len(village_list) * len(MODES) * len(OBJECTIVES)

    print(f"Named features: {len(runs_by_name)} runs, {len(lifts_by_name)} lifts")
    print(
        f"Matrix: {len(origins)} origins × {len(village_list)} villages × "
        f"{len(MODES)} modes × {len(OBJECTIVES)} objectives = {total_routes:,} routes"
    )

    t0 = time.perf_counter()
    edge_hits, failures = run_matrix(graph, origins, village_list)
    elapsed = time.perf_counter() - t0
    print(f"Matrix complete in {elapsed:.1f}s — {len(failures)} failures")

    html = render_html(
        graph, edge_hits, failures, runs_by_name, lifts_by_name,
        total_routes=total_routes, elapsed_s=elapsed,
    )
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
