"""Diagram the Contraction Hierarchy for Tignes/Val d'Isère.

Builds the CH, then renders an HTML map showing:

  * Every node colour-coded by its CH rank (importance):
      blue  = low rank (contracted early — "side streets")
      red   = high rank (contracted last  — "highways")
  * Shortcut edges drawn as dashed arcs in red — these are the synthetic
    edges that let a CH query skip over low-rank chains of nodes.
  * Original edges in light grey for context.
  * A side panel listing the top 20 "highway" nodes.

The shape that emerges: a sparse backbone of high-rank lift tops with
many shortcuts spanning them; the bulk of the graph is low-rank
intermediate piste segments that CH effectively bypasses.
"""

import json
import os
from collections import Counter
from pathlib import Path

import skiroute
from skiroute import contraction_hierarchies as ch
from skiroute.router import _make_cost_fn
from skiroute.config import RouteConfig


GPKG = Path(os.environ.get("SKIROUTE_GPKG", "openskidata.gpkg"))
OUT_DIR = Path(__file__).parent / "output"

TIGNES_VAL_DESTINATIONS = [
    skiroute.Destination("Tignes Val Claret",     lat=45.4510, lon=6.9000, radius_m=450, elev=2100),
    skiroute.Destination("Tignes Le Lac",         lat=45.4680, lon=6.9070, radius_m=500, elev=2100),
    skiroute.Destination("Tignes Les Boisses",    lat=45.4975, lon=6.9230, radius_m=500, elev=1800),
    skiroute.Destination("Tignes Les Brevieres",  lat=45.5080, lon=6.9210, radius_m=500, elev=1550),
    skiroute.Destination("Val d'Isere La Daille", lat=45.4608, lon=6.9638, radius_m=500, elev=1800),
    skiroute.Destination("Val d'Isere Centre",    lat=45.4490, lon=6.9810, radius_m=500, elev=1850),
    skiroute.Destination("Val d'Isere Le Fornet", lat=45.4500, lon=7.0110, radius_m=500, elev=1930),
]


def rank_to_colour(rank: int, max_rank: int) -> str:
    """Heatmap-style: blue (low rank) → yellow → red (high rank)."""
    t = rank / max(max_rank, 1)
    # piecewise linear from blue (#1e88e5) → yellow (#ffd200) → red (#e53935)
    if t < 0.5:
        s = t * 2
        r = int(0x1e + (0xff - 0x1e) * s)
        g = int(0x88 + (0xd2 - 0x88) * s)
        b = int(0xe5 + (0x00 - 0xe5) * s)
    else:
        s = (t - 0.5) * 2
        r = int(0xff + (0xe5 - 0xff) * s)
        g = int(0xd2 + (0x39 - 0xd2) * s)
        b = int(0x00 + (0x35 - 0x00) * s)
    return f"#{r:02x}{g:02x}{b:02x}"


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    print("Building Tignes graph...")
    graph = skiroute.build_graph(
        gpkg_path=GPKG,
        resort=skiroute.ResortFilter(ski_area_pattern="%Tignes - Val%"),
        destinations=TIGNES_VAL_DESTINATIONS,
        verbose=False,
    )
    print(f"  {graph}")

    cost_fn = _make_cost_fn("any-piste", "fastest", RouteConfig())

    print("Preprocessing CH...")
    chg = ch.preprocess(graph, cost_fn, verbose=False)
    print(f"  CH ready: {chg.n_shortcuts} shortcuts on top of {chg.n_original} originals")

    max_rank = max(chg.order.values())
    n_buckets = 5
    bucket_size = (max_rank + 1) / n_buckets
    buckets = Counter(int(r / bucket_size) for r in chg.order.values())
    print("\n  Rank distribution (split into 5 buckets):")
    for b in range(n_buckets):
        lo, hi = int(b * bucket_size), int((b + 1) * bucket_size) - 1
        print(f"    rank {lo:>3}-{hi:>3}: {buckets[b]:>3} nodes")

    # Top-20 highest-rank nodes ("highways")
    sorted_nodes = sorted(graph.nodes.values(), key=lambda n: -chg.order[n.id])
    print("\n  Top 20 highest-rank nodes — the 'highways':")
    for n in sorted_nodes[:20]:
        print(f"    rank {chg.order[n.id]:>3} · {n.elev:>5.0f}m · "
              f"{n.name or '(unnamed)'}")

    # --- Build the visualization data ---
    nodes_data = []
    for nid, n in graph.nodes.items():
        rank = chg.order[nid]
        nodes_data.append({
            "id": nid, "lat": n.lat, "lon": n.lon, "elev": n.elev,
            "name": n.name or f"node {nid}",
            "rank": rank,
            "colour": rank_to_colour(rank, max_rank),
        })

    # Original edges (light, for context)
    originals = []
    for e in graph.edges:
        a = graph.nodes[e.from_id]
        b = graph.nodes[e.to_id]
        originals.append({
            "coords": [[a.lat, a.lon], [b.lat, b.lon]],
            "type": e.type,
        })

    # Shortcuts (highlighted, dashed)
    shortcuts = []
    for ch_e in chg.edges:
        if not ch_e.is_shortcut:
            continue
        a = graph.nodes[ch_e.from_id]
        b = graph.nodes[ch_e.to_id]
        via = graph.nodes[ch_e.via_node]
        # min-rank of endpoints determines colour intensity — shortcuts
        # bridging high-rank nodes are more visually prominent
        min_rank = min(chg.order[ch_e.from_id], chg.order[ch_e.to_id])
        shortcuts.append({
            "coords": [[a.lat, a.lon], [b.lat, b.lon]],
            "via_lat": via.lat, "via_lon": via.lon, "via_name": via.name,
            "from_name": a.name or str(ch_e.from_id),
            "to_name": b.name or str(ch_e.to_id),
            "min_rank": min_rank,
        })

    # Top-rank nodes table
    top_table = []
    for n in sorted_nodes[:25]:
        top_table.append({
            "rank": chg.order[n.id], "name": n.name or "(unnamed)",
            "elev": int(n.elev), "lat": n.lat, "lon": n.lon,
        })

    # Centre map
    lats = [n["lat"] for n in nodes_data]
    lons = [n["lon"] for n in nodes_data]
    bbox = [[min(lats), min(lons)], [max(lats), max(lons)]]

    html = _TEMPLATE.format(
        bbox=json.dumps(bbox),
        nodes_json=json.dumps(nodes_data),
        originals_json=json.dumps(originals),
        shortcuts_json=json.dumps(shortcuts),
        top_table_json=json.dumps(top_table),
        n_nodes=len(graph.nodes),
        n_originals=chg.n_original,
        n_shortcuts=chg.n_shortcuts,
        preprocess_ms=int(chg.preprocess_seconds * 1000),
        max_rank=max_rank,
    )

    out = OUT_DIR / "tignes_ch_diagram.html"
    out.write_text(html)
    print(f"\nDiagram written to {out}")


_TEMPLATE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Tignes CH diagram</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  #map {{ height: 100vh; background:#0c1014; }}
  .panel {{
    position:absolute; top:12px; right:12px; z-index:1000;
    background:rgba(20,22,28,0.94); color:#eee; padding:14px 16px;
    border-radius:10px; box-shadow:0 6px 24px rgba(0,0,0,0.5);
    max-width:340px; font-size:13px; line-height:1.45;
  }}
  .panel h1 {{ font-size:15px; margin:0 0 6px 0; }}
  .panel h2 {{ font-size:12px; margin:14px 0 6px 0; color:#bbb;
              text-transform:uppercase; letter-spacing:0.08em; }}
  .summary {{ color:#aaa; font-size:12px; margin-bottom:8px; }}
  .legend-bar {{
    height:12px; border-radius:6px; margin:4px 0 2px;
    background: linear-gradient(to right, #1e88e5, #ffd200, #e53935);
  }}
  .legend-row {{ display:flex; justify-content:space-between; font-size:11px;
                color:#999; padding-bottom:6px; }}
  .controls label {{ display:block; cursor:pointer; padding:3px 0; font-size:12px; }}
  .top-table {{ font-size:11px; max-height:240px; overflow-y:auto;
                margin-top:6px; border-top:1px solid #444; padding-top:6px;
                font-family: ui-monospace, monospace; line-height:1.4; }}
  .top-table .row {{ display:flex; gap:6px; padding:1px 0; }}
  .top-table .rank {{ color:#e53935; font-weight:700; min-width:32px; }}
  .top-table .name {{ flex:1; overflow:hidden; text-overflow:ellipsis;
                       white-space:nowrap; }}
  .top-table .elev {{ color:#888; min-width:42px; text-align:right; }}
</style></head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>Tignes / Val d'Isère — CH diagram</h1>
  <div class="summary">
    {n_nodes} nodes · {n_originals} original edges · {n_shortcuts} shortcuts ·
    preprocessing {preprocess_ms} ms
  </div>

  <h2>Node rank (importance)</h2>
  <div class="legend-bar"></div>
  <div class="legend-row"><span>rank 0 (side streets)</span><span>rank {max_rank} (highways)</span></div>

  <div class="controls" style="margin-top:6px">
    <label><input type="checkbox" id="lyr-originals"> Show original edges</label>
    <label><input type="checkbox" id="lyr-shortcuts" checked> Show shortcut edges</label>
    <label><input type="checkbox" id="lyr-nodes" checked> Show node dots</label>
    <label><input type="checkbox" id="lyr-top-only"> Only show top-rank shortcuts</label>
  </div>

  <h2>Top 25 highways</h2>
  <div class="top-table" id="top-table"></div>
</div>
<script>
const bbox = {bbox};
const nodes = {nodes_json};
const originals = {originals_json};
const shortcuts = {shortcuts_json};
const topTable = {top_table_json};
const MAX_RANK = {max_rank};

const map = L.map('map', {{ preferCanvas: true }}).fitBounds(bbox);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 17, attribution: '© OpenStreetMap contributors', opacity: 0.55,
}}).addTo(map);

// Original edges — light grey context
const originalsLayer = L.layerGroup();
originals.forEach(e => {{
  L.polyline(e.coords, {{
    color: '#777', weight: 1, opacity: 0.35,
  }}).addTo(originalsLayer);
}});

// Shortcuts — dashed, coloured by min rank
const shortcutsAll = L.layerGroup();
const shortcutsTop = L.layerGroup();
const TOP_THRESHOLD = Math.floor(MAX_RANK * 0.85);
shortcuts.forEach(s => {{
  const t = s.min_rank / Math.max(1, MAX_RANK);
  const colour = `hsl(${{20 - 20*t}}, 90%, ${{50 + t*15}}%)`;
  const opacity = 0.25 + t * 0.5;
  const weight = 0.7 + t * 2.0;
  const line = L.polyline(s.coords, {{
    color: colour, weight, opacity,
    dashArray: '5 3',
  }}).bindTooltip(
    `<b>Shortcut</b><br>` +
    `${{s.from_name}} → ${{s.to_name}}<br>` +
    `<span style="color:#aaa">via ${{s.via_name || '(unnamed)'}}</span><br>` +
    `<span style="color:#888">min rank ${{s.min_rank}}</span>`
  );
  line.addTo(shortcutsAll);
  if (s.min_rank >= TOP_THRESHOLD) line.addTo(shortcutsTop);
}});

// Node markers
const nodesLayer = L.layerGroup();
nodes.forEach(n => {{
  const radius = 2.5 + (n.rank / MAX_RANK) * 6;
  L.circleMarker([n.lat, n.lon], {{
    radius, color: n.colour, weight: 1,
    fillColor: n.colour, fillOpacity: 0.9,
  }}).bindTooltip(
    `<b>${{n.name}}</b><br>${{Math.round(n.elev)}}m<br>` +
    `<span style="color:#888">CH rank ${{n.rank}}/${{MAX_RANK}}</span>`
  ).addTo(nodesLayer);
}});

shortcutsAll.addTo(map);
nodesLayer.addTo(map);

// Layer toggles
function bind(id, layerOn, layerOff) {{
  const el = document.getElementById(id);
  el.addEventListener('change', () => {{
    if (el.checked) {{
      if (layerOff && map.hasLayer(layerOff)) map.removeLayer(layerOff);
      if (layerOn) layerOn.addTo(map);
    }} else {{
      if (layerOn) map.removeLayer(layerOn);
    }}
  }});
}}
document.getElementById('lyr-originals').addEventListener('change', e => {{
  if (e.target.checked) originalsLayer.addTo(map);
  else map.removeLayer(originalsLayer);
}});
document.getElementById('lyr-nodes').addEventListener('change', e => {{
  if (e.target.checked) nodesLayer.addTo(map);
  else map.removeLayer(nodesLayer);
}});
const topOnly = document.getElementById('lyr-top-only');
const shortcutsCB = document.getElementById('lyr-shortcuts');
function updateShortcuts() {{
  map.removeLayer(shortcutsAll); map.removeLayer(shortcutsTop);
  if (!shortcutsCB.checked) return;
  if (topOnly.checked) shortcutsTop.addTo(map);
  else shortcutsAll.addTo(map);
}}
shortcutsCB.addEventListener('change', updateShortcuts);
topOnly.addEventListener('change', updateShortcuts);

// Top table
const tt = document.getElementById('top-table');
topTable.forEach((n, i) => {{
  const div = document.createElement('div');
  div.className = 'row';
  div.innerHTML = `<span class="rank">${{n.rank}}</span>
                   <span class="name" title="${{n.name}}">${{n.name}}</span>
                   <span class="elev">${{n.elev}}m</span>`;
  div.style.cursor = 'pointer';
  div.addEventListener('click', () => {{
    map.setView([n.lat, n.lon], 14);
  }});
  tt.appendChild(div);
}});
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
