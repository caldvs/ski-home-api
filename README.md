# Ski Home API

HTTP API for routing skiers home in the Tignes / Val d'Isere linked resort. Send your GPS coordinates and destination village, get back a structured route of runs, lifts, and skating connections.

Built on a precomputed routing graph from [ski-home](https://github.com/caldvs/ski-home), which extracts lift and run data from OpenSkiData and runs Dijkstra shortest-path routing with difficulty preferences.

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Interactive API docs at [localhost:8000/docs](http://localhost:8000/docs).

## Endpoints

### Routing

#### `GET /route`

Compute a route from GPS coordinates to a home village.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lon` | float | yes | Longitude of current position |
| `lat` | float | yes | Latitude of current position |
| `village` | string | yes | Destination village name |
| `difficulty` | string | no | `prefer-easy`, `reds-if-needed`, or `any-piste` (default) |
| `mode` | string | no | `direct` (fastest, default) or `most-skiing` (maximise vertical) |
| `via` | string | no | Waypoint name — route through a specific run or lift |
| `geometry` | bool | no | Include coordinate geometry for each leg (default false) |

```
GET /route?lon=6.98&lat=45.45&village=Tignes+Le+Lac&difficulty=prefer-easy
```

```json
{
  "from": {
    "name": "Solaise top",
    "lon": 6.9812,
    "lat": 45.4501,
    "elev": 2551.0,
    "snap_distance_m": 42.3
  },
  "to": {
    "village": "Tignes Le Lac",
    "name": "Rosset bottom",
    "elev": 2088.1
  },
  "difficulty": "prefer-easy",
  "summary": {
    "runs": 3,
    "lifts": 1,
    "estimated_minutes": 14,
    "vertical_m": 463.0
  },
  "legs": [
    {
      "type": "run",
      "name": "Piste L",
      "from": {"name": "Solaise top", "elev": 2551.0},
      "to": {"name": "Col de Fresse", "elev": 2363.8},
      "length_m": 1200.5,
      "estimated_seconds": 150.1,
      "difficulty": "easy",
      "colour": "blue"
    }
  ]
}
```

#### `GET /route/alternatives`

Return 2-3 alternative routes (fastest, most skiing, easiest terrain).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lon` | float | yes | Longitude |
| `lat` | float | yes | Latitude |
| `village` | string | yes | Destination village |
| `difficulty` | string | no | Difficulty preference |
| `geometry` | bool | no | Include geometry |

```
GET /route/alternatives?lon=6.98&lat=45.45&village=Tignes+Le+Lac
```

### Location

#### `GET /nearby`

Find runs, lifts, and villages near GPS coordinates.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lon` | float | yes | Longitude |
| `lat` | float | yes | Latitude |
| `radius_m` | float | no | Search radius in metres (default 500) |

```
GET /nearby?lon=6.98&lat=45.45&radius_m=300
```

#### `GET /status`

Describe where you are: nearest node, adjacent runs and lifts, reachable villages with estimated times.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lon` | float | yes | Longitude |
| `lat` | float | yes | Latitude |

```
GET /status?lon=6.98&lat=45.45
```

### Discovery

#### `GET /runs`

List all runs in the resort with difficulty, length, and elevation drop.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `search` | string | no | Filter by name (case-insensitive) |
| `difficulty` | string | no | Filter by difficulty (`novice`, `easy`, `intermediate`, `advanced`, `expert`) |

```
GET /runs?difficulty=intermediate
```

#### `GET /lifts`

List all lifts with type and elevation gain.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `search` | string | no | Filter by name (case-insensitive) |
| `lift_type` | string | no | Filter by type (`chair_lift`, `gondola`, `funicular`, `cable_car`, `drag_lift`, etc.) |

```
GET /lifts?lift_type=gondola
```

#### `GET /graph`

Return the raw routing graph (nodes, edges, villages) for custom clients.

```
GET /graph
```

### Reference

#### `GET /villages`

List available destination villages with coordinates.

```
GET /villages
```

## Data sources

Ski and lift geometry is sourced from [OpenSkiMap](https://openskimap.org) via the [OpenSkiData](https://openskidata.org) project, which is derived from [OpenStreetMap](https://www.openstreetmap.org) data.

- **OpenStreetMap** — [Open Data Commons Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/)
- **OpenSkiData** — [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/)
