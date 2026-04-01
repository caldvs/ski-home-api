# Ski Home API

HTTP API for routing skiers home in the Tignes / Val d'Isere linked resort. Send your GPS coordinates and destination village, get back a structured route of runs, lifts, and skating connections.

Built on a precomputed routing graph from [ski-home](https://github.com/caldvs/ski-home), which extracts lift and run data from OpenSkiData and runs Dijkstra shortest-path routing with difficulty preferences.

**Live API:** [ski-home-api.onrender.com](https://ski-home-api.onrender.com)

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Interactive API docs at [localhost:8000/docs](http://localhost:8000/docs) (Swagger) or [localhost:8000](http://localhost:8000) (Stoplight Elements).

## Resort coverage

The graph covers the full Tignes / Val d'Isere linked ski area:

- **374 nodes** (lift stations, run endpoints)
- **1311 edges** (runs, lifts, skating connections)
- **8 villages** across two valleys, 1550m to 3450m elevation
- **3 difficulty modes**: prefer easy (greens/blues), reds if needed, any piste

## Endpoints

### Routing

#### `GET /route`

Compute a route from GPS coordinates to a home village. The router snaps your position to the nearest graph node, runs Dijkstra with difficulty-adjusted edge costs, and returns an ordered sequence of legs.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lon` | float | 6.9258 | Longitude of current position |
| `lat` | float | 45.4421 | Latitude of current position |
| `village` | string | Tignes Le Lac | Destination village name (see `/villages` for options) |
| `difficulty` | string | `any-piste` | `prefer-easy`, `reds-if-needed`, or `any-piste` |
| `mode` | string | `direct` | `direct` (fastest) or `most-skiing` (maximise vertical metres) |
| `via` | string | — | Optional waypoint — route through a specific node by name (case-insensitive partial match) |
| `geometry` | bool | false | Include `[lon, lat]` coordinate arrays for each leg (for map rendering) |

**Example:**

```
GET /route?lon=6.98&lat=45.45&village=Tignes+Le+Lac&difficulty=prefer-easy
```

**Response:**

```json
{
  "from": {
    "name": "Village bottom",
    "lon": 6.978204,
    "lat": 45.447549,
    "elev": 1832.6,
    "snap_distance_m": 306.4
  },
  "to": {
    "village": "Tignes Le Lac",
    "name": "Rosset bottom",
    "elev": 2086.1
  },
  "difficulty": "prefer-easy",
  "summary": {
    "runs": 8,
    "lifts": 1,
    "estimated_minutes": 35,
    "vertical_m": 1005.0
  },
  "legs": [
    {
      "type": "run",
      "name": "Henri",
      "from": {"name": "Henri top", "elev": 2692.0},
      "to": {"name": "Henri bottom", "elev": 2129.0},
      "length_m": 2450.3,
      "estimated_seconds": 306.3,
      "difficulty": "easy",
      "colour": "blue"
    },
    {
      "type": "lift",
      "name": "Tufs",
      "from": {"name": "Tufs bottom", "elev": 2129.0},
      "to": {"name": "Tufs top", "elev": 2692.0},
      "length_m": 1820.5,
      "estimated_seconds": 360.0,
      "lift_type": "chair_lift",
      "direction": "up"
    },
    {
      "type": "skate",
      "name": "Skate 95m",
      "from": {"name": "Node A", "elev": 2100.0},
      "to": {"name": "Node B", "elev": 2105.0},
      "length_m": 95.0,
      "estimated_seconds": 30.0,
      "elev_gain": 5.0
    }
  ]
}
```

**Leg types:**

| Type | Description | Extra fields |
|------|-------------|--------------|
| `run` | Downhill piste | `difficulty`, `colour` (green/blue/red/black) |
| `connection` | Named piste connection | `difficulty`, `colour` |
| `lift` | Uphill lift | `lift_type`, `direction` ("up") |
| `lift_down` | Riding a lift downhill | `lift_type`, `direction` ("down") |
| `skate` | Gap-bridging skating connection | `elev_gain` (if uphill) |

**Difficulty modes:**

| Mode | Behaviour |
|------|-----------|
| `prefer-easy` | Heavy penalty on red/black runs. Will use lift-down if needed to avoid hard terrain. |
| `reds-if-needed` | Moderate penalty on black runs. Uses reds freely. |
| `any-piste` | No difficulty penalty. Fastest route regardless of terrain. |

---

#### `GET /route/alternatives`

Return 2-3 alternative routes using different strategies, so the user can compare options.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lon` | float | 6.9258 | Longitude |
| `lat` | float | 45.4421 | Latitude |
| `village` | string | Tignes Le Lac | Destination village |
| `difficulty` | string | `any-piste` | Difficulty preference |
| `geometry` | bool | false | Include geometry |

**Strategies returned:**

| Label | Strategy |
|-------|----------|
| `fastest` | Shortest time (direct mode) |
| `most-skiing` | Maximise vertical metres skied on the way home |
| `easiest` | Prefer easy terrain regardless of caller's difficulty setting |

Duplicate routes (where two strategies produce the same path) are omitted.

**Example:**

```
GET /route/alternatives?lon=6.98&lat=45.45&village=Tignes+Le+Lac
```

**Response:**

```json
{
  "alternatives": [
    {
      "label": "fastest",
      "from": { "name": "Village bottom", "lon": 6.978204, "lat": 45.447549, "elev": 1832.6, "snap_distance_m": 306.4 },
      "to": { "village": "Tignes Le Lac", "name": "Rosset bottom", "elev": 2086.1 },
      "difficulty": "any-piste",
      "summary": { "runs": 6, "lifts": 1, "estimated_minutes": 28, "vertical_m": 850.0 },
      "legs": [ ]
    },
    {
      "label": "easiest",
      "from": { "name": "Village bottom", "lon": 6.978204, "lat": 45.447549, "elev": 1832.6, "snap_distance_m": 306.4 },
      "to": { "village": "Tignes Le Lac", "name": "Rosset bottom", "elev": 2086.1 },
      "difficulty": "prefer-easy",
      "summary": { "runs": 8, "lifts": 1, "estimated_minutes": 35, "vertical_m": 1005.0 },
      "legs": [ ]
    }
  ]
}
```

---

### Location

#### `GET /nearby`

Find runs, lifts, and villages near a GPS position. Useful for "what's around me?" queries. Runs and lifts are found within the given radius; villages use a wider radius (5x) so you can always see your options.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lon` | float | 6.9258 | Longitude |
| `lat` | float | 45.4421 | Latitude |
| `radius_m` | float | 500 | Search radius in metres for runs and lifts |

**Example:**

```
GET /nearby?lon=6.98&lat=45.45&radius_m=500
```

**Response:**

```json
{
  "runs": [
    {
      "name": "Run 103696",
      "difficulty": "easy",
      "colour": "blue",
      "from": {"name": "Solaise bottom", "elev": 1832.2},
      "to": {"name": "Olympique bottom", "elev": 1833.0},
      "length_m": 29.6,
      "distance_m": 400.6
    }
  ],
  "lifts": [
    {
      "name": "Village",
      "lift_type": "chair_lift",
      "from": {"name": "Village bottom", "elev": 1832.6},
      "to": {"name": "Village top", "elev": 1882.6},
      "distance_m": 306.4
    }
  ],
  "villages": [
    {"name": "Val d'Isere Centre", "distance_m": 1200.0, "elev": 1850},
    {"name": "Val d'Isere La Daille", "distance_m": 1800.0, "elev": 1800}
  ]
}
```

---

#### `GET /status`

Describe where you are on the mountain. Returns your snapped position, what runs and lifts are immediately accessible (adjacent graph edges), and which villages are reachable with estimated times.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lon` | float | 6.9258 | Longitude |
| `lat` | float | 45.4421 | Latitude |

**Example:**

```
GET /status?lon=6.98&lat=45.45
```

**Response:**

```json
{
  "position": {
    "nearest_node": "Village bottom",
    "lon": 6.978204,
    "lat": 45.447549,
    "elev": 1832.6,
    "snap_distance_m": 306.4,
    "villages": ["Val d'Isere La Daille", "Val d'Isere Centre"]
  },
  "adjacent": {
    "runs": [],
    "lifts": [
      {"name": "Village", "lift_type": "chair_lift"}
    ]
  },
  "reachable_villages": [
    {"village": "Val d'Isere La Daille", "estimated_minutes": 5, "runs": 1, "lifts": 0},
    {"village": "Val d'Isere Centre", "estimated_minutes": 8, "runs": 2, "lifts": 0},
    {"village": "Tignes Le Lac", "estimated_minutes": 28, "runs": 6, "lifts": 1},
    {"village": "Tignes Val Claret", "estimated_minutes": 30, "runs": 7, "lifts": 1}
  ]
}
```

---

### Discovery

#### `GET /runs`

List all runs in the resort. Each run includes its difficulty, colour, length, and elevation drop. Results are sorted alphabetically by name.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `search` | string | — | Filter by name (case-insensitive partial match) |
| `difficulty` | string | — | Filter by difficulty: `novice`, `easy`, `intermediate`, `advanced`, `expert`, `freeride` |

**Example:**

```
GET /runs?difficulty=intermediate
```

**Response:**

```json
{
  "count": 42,
  "runs": [
    {
      "name": "Ancolie",
      "difficulty": "intermediate",
      "colour": "red",
      "from": {"name": "Ancolie top", "elev": 2565.6},
      "to": {"name": "Aiguille Percée bottom", "elev": 2426.4},
      "length_m": 607.4,
      "elev_drop": 140.1
    },
    {
      "name": "Raye",
      "difficulty": "intermediate",
      "colour": "red",
      "from": {"name": "Raye top", "elev": 2018.8},
      "to": {"name": "Raye bottom", "elev": 1802.7},
      "length_m": 728.7,
      "elev_drop": 216.1
    }
  ]
}
```

**Difficulty values:**

| Value | Colour | Description |
|-------|--------|-------------|
| `novice` | Green | Beginner |
| `easy` | Blue | Easy |
| `intermediate` | Red | Intermediate |
| `advanced` | Black | Advanced |
| `expert` | Black | Expert / marked itinerary |
| `freeride` | Orange | Off-piste |

---

#### `GET /lifts`

List all lifts in the resort. Each lift includes its type, length, and elevation gain. Results are sorted alphabetically by name.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `search` | string | — | Filter by name (case-insensitive partial match) |
| `lift_type` | string | — | Filter by type (see table below) |

**Example:**

```
GET /lifts?lift_type=gondola
```

**Response:**

```json
{
  "count": 6,
  "lifts": [
    {
      "name": "TCD10 des Boisses",
      "lift_type": "gondola",
      "from": {"name": "TCD10 des Boisses bottom", "elev": 1769.2},
      "to": {"name": "TCD10 des Boisses top", "elev": 2179.8},
      "length_m": 1087.6,
      "elev_gain": 410.6
    },
    {
      "name": "TCD8 des Brévières",
      "lift_type": "gondola",
      "from": {"name": "TCD8 des Brévières bottom", "elev": 1558.8},
      "to": {"name": "TCD10 des Boisses bottom", "elev": 1769.2},
      "length_m": 892.2,
      "elev_gain": 210.4
    }
  ]
}
```

**Lift types:**

| Type | Description |
|------|-------------|
| `chair_lift` | Chairlift (2-8 seats) |
| `gondola` | Enclosed gondola cabin |
| `cable_car` | Large aerial tramway |
| `funicular` | Funicular railway |
| `drag_lift` | Button/poma lift |
| `t-bar` | T-bar drag lift |
| `platter` | Platter/button lift |
| `rope_tow` | Rope tow |

---

#### `GET /graph`

Return the complete routing graph for custom clients. This is the same data the API uses internally for routing — nodes with coordinates and village membership, edges with cost, geometry, difficulty, and type.

```
GET /graph
```

**Response structure:**

```json
{
  "nodes": [
    {
      "id": 0,
      "lon": 6.881624,
      "lat": 45.451951,
      "elev": 2427.2,
      "name": "TSD6 Grattalu bottom",
      "villages": []
    }
  ],
  "edges": [
    {
      "from": 0,
      "to": 1,
      "type": "lift",
      "name": "TSD6 Grattalu",
      "cost_base": 360.0,
      "length_m": 1200.0,
      "elev_drop": -318.9,
      "difficulty": null,
      "lift_type": "chair_lift",
      "geometry": [[6.881624, 45.451951], [6.870346, 45.461252]]
    }
  ],
  "villages": {
    "Tignes Val Claret": {"lat": 45.451, "lon": 6.9, "elev": 2100}
  }
}
```

---

### Reference

#### `GET /villages`

List available destination villages with their centre coordinates and elevation.

```
GET /villages
```

**Response:**

```json
{
  "Tignes Val Claret": {"lat": 45.451, "lon": 6.9, "elev": 2100},
  "Tignes Le Lac": {"lat": 45.468, "lon": 6.907, "elev": 2100},
  "Tignes Les Boisses": {"lat": 45.4975, "lon": 6.923, "elev": 1800},
  "Tignes Les Brevieres": {"lat": 45.508, "lon": 6.921, "elev": 1550},
  "Val d'Isere La Daille": {"lat": 45.4608, "lon": 6.9638, "elev": 1800},
  "Val d'Isere Centre": {"lat": 45.449, "lon": 6.981, "elev": 1850},
  "Val d'Isere Le Laisinant": {"lat": 45.4471, "lon": 6.9943, "elev": 1860},
  "Val d'Isere Le Fornet": {"lat": 45.45, "lon": 7.011, "elev": 1930}
}
```

## Data sources

Ski and lift geometry is sourced from [OpenSkiMap](https://openskimap.org) via the [OpenSkiData](https://openskidata.org) project, which is derived from [OpenStreetMap](https://www.openstreetmap.org) data.

- **OpenStreetMap** — [Open Data Commons Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/)
- **OpenSkiData** — [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/)
