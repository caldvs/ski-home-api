# Ski Home API

HTTP API for routing skiers home in the Tignes / Val d'Isere linked resort. Send your GPS coordinates and destination village, get back a structured route of runs, lifts, and skating connections.

Built on a precomputed routing graph from [ski-home](https://github.com/caldvs/ski-home), which extracts lift and run data from OpenSkiData and runs Dijkstra shortest-path routing with difficulty preferences.

## Quickstart

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Interactive docs at [localhost:8000/docs](http://localhost:8000/docs).

## Endpoints

### `GET /route`

Compute a route from GPS coordinates to a home village.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lon` | float | yes | Longitude of current position |
| `lat` | float | yes | Latitude of current position |
| `village` | string | yes | Destination village name |
| `difficulty` | string | no | `prefer-easy`, `reds-if-needed`, or `any-piste` (default) |

**Example:**

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
    "estimated_minutes": 14
  },
  "legs": [
    {
      "type": "run",
      "name": "Piste L",
      "from": {"name": "Solaise top", "elev": 2551.0},
      "to": {"name": "Col de Fresse", "elev": 2363.8},
      "length_m": 1200.5,
      "difficulty": "easy",
      "colour": "blue"
    }
  ]
}
```

### `GET /villages`

List available destination villages with coordinates.

```
GET /villages
```

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
