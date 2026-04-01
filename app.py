"""
Ski Home API — Route skiers home in Tignes / Val d'Isere.

Loads a precomputed routing graph and exposes Dijkstra shortest-path
routing over HTTP. Send your GPS coordinates and destination village,
get back a structured route of runs, lifts, and skating connections.
"""

import json
import math
import heapq
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse

GRAPH_PATH = Path(__file__).parent / "graph.json"

DIFFICULTY_COLOUR = {
    "novice": "green",
    "easy": "blue",
    "intermediate": "red",
    "advanced": "black",
    "expert": "black",
    "freeride": "orange",
}

DIFFICULTY_PENALTY = {
    "prefer-easy": {
        "novice": 0, "easy": 0, "intermediate": 600, "advanced": 2000,
        "expert": 5000, "freeride": 5000,
    },
    "reds-if-needed": {
        "novice": 0, "easy": 0, "intermediate": 60, "advanced": 800,
        "expert": 3000, "freeride": 3000,
    },
    "any-piste": {
        "novice": 0, "easy": 0, "intermediate": 0, "advanced": 0,
        "expert": 60, "freeride": 120,
    },
}

LIFT_DOWN_PENALTY = 600

# For "most skiing" mode: invert the preference so the router favours
# longer runs and more vertical. We negate elev_drop as a bonus.
MOST_SKIING_RUN_BONUS_PER_M = 0.5  # seconds of bonus per metre of vertical


# ---------------------------------------------------------------------------
# Graph data structures
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: int
    lon: float
    lat: float
    elev: float
    name: str
    villages: list[str] = field(default_factory=list)


@dataclass
class Edge:
    from_id: int
    to_id: int
    type: str
    name: str
    cost_base: float
    length_m: float
    elev_drop: float
    difficulty: str = ""
    lift_type: str = ""
    geometry: list[list[float]] = field(default_factory=list)


def haversine(lon1, lat1, lon2, lat2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class SkiGraph:
    def __init__(self, path: Path):
        with open(path) as f:
            data = json.load(f)

        self.nodes: dict[int, Node] = {}
        for n in data["nodes"]:
            self.nodes[n["id"]] = Node(
                id=n["id"], lon=n["lon"], lat=n["lat"],
                elev=n["elev"], name=n["name"], villages=n.get("villages", []),
            )

        self.edges: list[Edge] = []
        self.adjacency: dict[int, list[int]] = {}
        for e in data["edges"]:
            idx = len(self.edges)
            self.edges.append(Edge(
                from_id=e["from"], to_id=e["to"], type=e["type"],
                name=e["name"], cost_base=e["cost_base"],
                length_m=e.get("length_m", 0), elev_drop=e.get("elev_drop", 0),
                difficulty=e.get("difficulty", ""), lift_type=e.get("lift_type", ""),
                geometry=e.get("geometry", []),
            ))
            self.adjacency.setdefault(e["from"], []).append(idx)

        self.villages = data.get("villages", {})

        # Build indexes for /runs, /lifts, /nearby lookups
        self._run_edges: list[int] = []
        self._lift_edges: list[int] = []
        for i, e in enumerate(self.edges):
            if e.type in ("run", "connection"):
                self._run_edges.append(i)
            elif e.type in ("lift", "lift_down"):
                self._lift_edges.append(i)

    def find_nearest_node(self, lon: float, lat: float) -> tuple[Node, float]:
        best, best_dist = None, float("inf")
        for node in self.nodes.values():
            d = haversine(lon, lat, node.lon, node.lat)
            if d < best_dist:
                best_dist = d
                best = node
        return best, best_dist

    def find_nearby_nodes(self, lon: float, lat: float, radius_m: float) -> list[tuple[Node, float]]:
        results = []
        for node in self.nodes.values():
            d = haversine(lon, lat, node.lon, node.lat)
            if d <= radius_m:
                results.append((node, d))
        results.sort(key=lambda x: x[1])
        return results

    def route(self, from_node_id: int, to_village: str, difficulty: str = "any-piste",
              mode: str = "direct", via_node_id: int | None = None):
        """Route from a node to a village.

        mode: "direct" (fastest), "most-skiing" (maximise vertical)
        via_node_id: optional waypoint node to route through
        """
        if via_node_id is not None:
            # Route in two segments: from -> via, via -> village
            path1, err1 = self._dijkstra(from_node_id, {via_node_id}, difficulty, mode)
            if err1:
                return None, f"Cannot reach waypoint: {err1}"
            target_nodes = {n.id for n in self.nodes.values() if to_village in n.villages}
            if not target_nodes:
                return None, f"No nodes found in village '{to_village}'"
            path2, err2 = self._dijkstra(via_node_id, target_nodes, difficulty, mode)
            if err2:
                return None, f"Cannot reach village from waypoint: {err2}"
            path = path1 + path2
            # Extend through village
            path = self._extend_through_village(path, to_village)
            return path, None

        target_nodes = {n.id for n in self.nodes.values() if to_village in n.villages}
        if not target_nodes:
            return None, f"No nodes found in village '{to_village}'"

        path, err = self._dijkstra(from_node_id, target_nodes, difficulty, mode)
        if err:
            return path, err
        path = self._extend_through_village(path, to_village)
        return path, None

    def _dijkstra(self, from_node_id: int, target_nodes: set[int],
                  difficulty: str, mode: str):
        penalties = DIFFICULTY_PENALTY.get(difficulty, DIFFICULTY_PENALTY["any-piste"])

        dist = {nid: float("inf") for nid in self.nodes}
        dist[from_node_id] = 0
        prev = {}
        prev_edge = {}
        visited = set()
        heap = [(0, from_node_id)]

        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)

            if u in target_nodes:
                path = []
                current = u
                while current in prev_edge:
                    path.append(prev_edge[current])
                    current = prev[current]
                path.reverse()
                return path, None

            for edge_idx in self.adjacency.get(u, []):
                edge = self.edges[edge_idx]
                v = edge.to_id
                if v in visited:
                    continue

                cost = edge.cost_base
                if edge.difficulty and edge.type in ("run", "connection"):
                    cost += penalties.get(edge.difficulty, 0)
                if edge.type == "lift_down" and difficulty == "prefer-easy":
                    cost -= LIFT_DOWN_PENALTY * 0.7

                # In most-skiing mode, reward vertical on runs
                if mode == "most-skiing" and edge.type in ("run", "connection"):
                    cost -= edge.elev_drop * MOST_SKIING_RUN_BONUS_PER_M

                new_dist = d + cost
                if new_dist < dist[v]:
                    dist[v] = new_dist
                    prev[v] = u
                    prev_edge[v] = edge
                    heapq.heappush(heap, (new_dist, v))

        return None, "No route found (target unreachable)"

    def _extend_through_village(self, path: list[Edge], to_village: str) -> list[Edge]:
        if not path:
            return path
        cursor = path[-1].to_id
        while True:
            best_edge, best_drop = None, 0
            for eidx in self.adjacency.get(cursor, []):
                e = self.edges[eidx]
                if e.type not in ("run", "connection"):
                    continue
                dest = self.nodes[e.to_id]
                if to_village not in dest.villages:
                    continue
                if dest.elev < self.nodes[cursor].elev and e.elev_drop > best_drop:
                    best_edge = e
                    best_drop = e.elev_drop
            if best_edge is None:
                break
            path.append(best_edge)
            cursor = best_edge.to_id
        return path

    def route_alternatives(self, from_node_id: int, to_village: str,
                           difficulty: str = "any-piste", count: int = 3):
        """Return up to `count` alternative routes by varying strategy."""
        routes = []

        # 1. Direct (fastest)
        path, err = self.route(from_node_id, to_village, difficulty, mode="direct")
        if path:
            routes.append(("fastest", path))

        # 2. Most skiing (max vertical)
        path2, _ = self.route(from_node_id, to_village, difficulty, mode="most-skiing")
        if path2 and _edge_ids(path2) != _edge_ids(routes[0][1] if routes else []):
            routes.append(("most-skiing", path2))

        # 3. Easiest terrain
        if difficulty != "prefer-easy":
            path3, _ = self.route(from_node_id, to_village, "prefer-easy", mode="direct")
            if path3 and all(_edge_ids(path3) != _edge_ids(r) for _, r in routes):
                routes.append(("easiest", path3))

        return routes[:count]


def _edge_ids(path: list[Edge]) -> tuple:
    return tuple((e.from_id, e.to_id) for e in path)


# ---------------------------------------------------------------------------
# Leg consolidation
# ---------------------------------------------------------------------------

def consolidate_legs(graph: SkiGraph, path: list[Edge],
                     include_geometry: bool = False) -> list[dict]:
    if not path:
        return []

    legs = []
    current_edges = [path[0]]
    current_type = path[0].type
    current_name = path[0].name

    for edge in path[1:]:
        same_run = (edge.name == current_name and edge.type == current_type
                    and edge.type in ("run", "connection"))
        same_skate = edge.type == "skate" and current_type == "skate"
        if same_run or same_skate:
            current_edges.append(edge)
        else:
            legs.append(_build_leg(graph, current_edges, current_type, current_name,
                                   include_geometry))
            current_edges = [edge]
            current_type = edge.type
            current_name = edge.name

    legs.append(_build_leg(graph, current_edges, current_type, current_name,
                           include_geometry))
    return legs


def _build_leg(graph: SkiGraph, edges: list[Edge], leg_type: str, name: str,
               include_geometry: bool = False) -> dict:
    first, last = edges[0], edges[-1]
    from_node = graph.nodes[first.from_id]
    to_node = graph.nodes[last.to_id]

    leg = {
        "type": leg_type,
        "name": name,
        "from": {"name": from_node.name, "elev": from_node.elev},
        "to": {"name": to_node.name, "elev": to_node.elev},
        "length_m": round(sum(e.length_m for e in edges), 1),
        "estimated_seconds": round(sum(e.cost_base for e in edges), 1),
    }

    if leg_type in ("run", "connection") and first.difficulty:
        leg["difficulty"] = first.difficulty
        leg["colour"] = DIFFICULTY_COLOUR.get(first.difficulty, "unknown")
    elif leg_type in ("lift", "lift_down"):
        leg["lift_type"] = first.lift_type
        leg["direction"] = "down" if leg_type == "lift_down" else "up"
    elif leg_type == "skate":
        leg["name"] = f"Skate {leg['length_m']:.0f}m"
        elev_gain = max(0, to_node.elev - from_node.elev)
        if elev_gain > 0:
            leg["elev_gain"] = round(elev_gain, 1)

    if include_geometry:
        coords = []
        for e in edges:
            if e.geometry:
                # Avoid duplicating the join point between consecutive edges
                if coords and e.geometry[0] == coords[-1]:
                    coords.extend(e.geometry[1:])
                else:
                    coords.extend(e.geometry)
        leg["geometry"] = coords

    return leg


# ---------------------------------------------------------------------------
# Route response builder
# ---------------------------------------------------------------------------

def _build_route_response(path: list[Edge], node: Node, dist_m: float,
                          village: str, difficulty: str,
                          include_geometry: bool = False,
                          label: str | None = None) -> dict:
    legs = consolidate_legs(graph, path, include_geometry)
    last_node = graph.nodes[path[-1].to_id] if path else node

    run_count = sum(1 for l in legs if l["type"] in ("run", "connection"))
    lift_count = sum(1 for l in legs if l["type"] in ("lift", "lift_down"))
    total_time_s = sum(e.cost_base for e in path)
    total_vertical = round(sum(e.elev_drop for e in path if e.type in ("run", "connection")), 1)

    resp = {
        "from": {
            "name": node.name,
            "lon": node.lon,
            "lat": node.lat,
            "elev": node.elev,
            "snap_distance_m": round(dist_m, 1),
        },
        "to": {
            "village": village,
            "name": last_node.name,
            "elev": last_node.elev,
        },
        "difficulty": difficulty,
        "summary": {
            "runs": run_count,
            "lifts": lift_count,
            "estimated_minutes": round(total_time_s / 60),
            "vertical_m": total_vertical,
        },
        "legs": legs,
    }
    if label:
        resp["label"] = label
    return resp


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

graph = SkiGraph(GRAPH_PATH)

app = FastAPI(
    title="Ski Home API",
    description="Route skiers home in the Tignes / Val d'Isere linked resort.",
    version="1.0.0",
)


class Difficulty(str, Enum):
    prefer_easy = "prefer-easy"
    reds_if_needed = "reds-if-needed"
    any_piste = "any-piste"


class RouteMode(str, Enum):
    direct = "direct"
    most_skiing = "most-skiing"


@app.get("/")
def root():
    return RedirectResponse(url="/docs")


@app.get("/villages")
def villages():
    """List available home villages with their coordinates."""
    return {
        name: {"lat": v["lat"], "lon": v["lon"], "elev": v["elev"]}
        for name, v in graph.villages.items()
    }


@app.get("/route")
def route(
    lon: float = Query(default=6.9258, description="Longitude of current position"),
    lat: float = Query(default=45.4421, description="Latitude of current position"),
    village: str = Query(default="Tignes Le Lac", description="Destination village name"),
    difficulty: Difficulty = Query(
        default=Difficulty.any_piste,
        description="Difficulty preference",
    ),
    mode: RouteMode = Query(
        default=RouteMode.direct,
        description="Routing mode: 'direct' (fastest) or 'most-skiing' (maximise vertical)",
    ),
    via: str | None = Query(
        default=None,
        description="Optional waypoint — name of a run or lift to route through",
    ),
    geometry: bool = Query(
        default=False,
        description="Include coordinate geometry for each leg",
    ),
):
    """Compute a route from GPS coordinates to a home village."""
    if village not in graph.villages:
        return {"error": f"Unknown village '{village}'", "villages": list(graph.villages.keys())}

    node, dist_m = graph.find_nearest_node(lon, lat)

    # Resolve waypoint
    via_node_id = None
    if via:
        via_matches = [n for n in graph.nodes.values() if via.lower() in n.name.lower()]
        if not via_matches:
            return {"error": f"No node found matching waypoint '{via}'"}
        via_node_id = via_matches[0].id

    path, err = graph.route(node.id, village, difficulty.value,
                            mode=mode.value, via_node_id=via_node_id)

    if err:
        return {"error": err}

    return _build_route_response(path, node, dist_m, village, difficulty.value, geometry)


@app.get("/route/alternatives")
def route_alternatives(
    lon: float = Query(default=6.9258, description="Longitude of current position"),
    lat: float = Query(default=45.4421, description="Latitude of current position"),
    village: str = Query(default="Tignes Le Lac", description="Destination village name"),
    difficulty: Difficulty = Query(
        default=Difficulty.any_piste,
        description="Difficulty preference",
    ),
    geometry: bool = Query(
        default=False,
        description="Include coordinate geometry for each leg",
    ),
):
    """Return 2-3 alternative routes ranked by different strategies."""
    if village not in graph.villages:
        return {"error": f"Unknown village '{village}'", "villages": list(graph.villages.keys())}

    node, dist_m = graph.find_nearest_node(lon, lat)
    alternatives = graph.route_alternatives(node.id, village, difficulty.value)

    return {
        "alternatives": [
            _build_route_response(path, node, dist_m, village, difficulty.value,
                                  geometry, label=label)
            for label, path in alternatives
        ]
    }


@app.get("/nearby")
def nearby(
    lon: float = Query(default=6.9258, description="Longitude of current position"),
    lat: float = Query(default=45.4421, description="Latitude of current position"),
    radius_m: float = Query(default=500, description="Search radius in metres"),
):
    """Find nearby runs, lifts, and villages from GPS coordinates."""
    nearby_nodes = graph.find_nearby_nodes(lon, lat, radius_m)

    runs = []
    lifts = []
    seen_runs = set()
    seen_lifts = set()

    for node, dist in nearby_nodes:
        for eidx in graph.adjacency.get(node.id, []):
            edge = graph.edges[eidx]
            if edge.type in ("run", "connection") and edge.name and edge.name not in seen_runs:
                seen_runs.add(edge.name)
                to_node = graph.nodes[edge.to_id]
                runs.append({
                    "name": edge.name,
                    "difficulty": edge.difficulty,
                    "colour": DIFFICULTY_COLOUR.get(edge.difficulty, ""),
                    "from": {"name": node.name, "elev": node.elev},
                    "to": {"name": to_node.name, "elev": to_node.elev},
                    "length_m": round(edge.length_m, 1),
                    "distance_m": round(dist, 1),
                })
            elif edge.type == "lift" and edge.name and edge.name not in seen_lifts:
                seen_lifts.add(edge.name)
                to_node = graph.nodes[edge.to_id]
                lifts.append({
                    "name": edge.name,
                    "lift_type": edge.lift_type,
                    "from": {"name": node.name, "elev": node.elev},
                    "to": {"name": to_node.name, "elev": to_node.elev},
                    "distance_m": round(dist, 1),
                })

    # Nearby villages
    nearby_villages = []
    for name, v in graph.villages.items():
        d = haversine(lon, lat, v["lon"], v["lat"])
        if d <= radius_m * 5:  # villages in wider radius
            nearby_villages.append({
                "name": name,
                "distance_m": round(d, 1),
                "elev": v["elev"],
            })
    nearby_villages.sort(key=lambda x: x["distance_m"])

    return {
        "runs": sorted(runs, key=lambda x: x["distance_m"]),
        "lifts": sorted(lifts, key=lambda x: x["distance_m"]),
        "villages": nearby_villages,
    }


@app.get("/status")
def status(
    lon: float = Query(default=6.9258, description="Longitude of current position"),
    lat: float = Query(default=45.4421, description="Latitude of current position"),
):
    """Describe where you are on the mountain: nearest node, area, reachable villages."""
    node, dist_m = graph.find_nearest_node(lon, lat)

    # Which villages are reachable?
    reachable = []
    for village_name in graph.villages:
        path, err = graph.route(node.id, village_name, "any-piste")
        if path:
            total_time_s = sum(e.cost_base for e in path)
            legs = consolidate_legs(graph, path)
            run_count = sum(1 for l in legs if l["type"] in ("run", "connection"))
            lift_count = sum(1 for l in legs if l["type"] in ("lift", "lift_down"))
            reachable.append({
                "village": village_name,
                "estimated_minutes": round(total_time_s / 60),
                "runs": run_count,
                "lifts": lift_count,
            })
    reachable.sort(key=lambda x: x["estimated_minutes"])

    # What's immediately accessible (adjacent edges)
    adjacent_runs = []
    adjacent_lifts = []
    for eidx in graph.adjacency.get(node.id, []):
        e = graph.edges[eidx]
        if e.type in ("run", "connection") and e.name:
            adjacent_runs.append({
                "name": e.name,
                "difficulty": e.difficulty,
                "colour": DIFFICULTY_COLOUR.get(e.difficulty, ""),
            })
        elif e.type == "lift" and e.name:
            adjacent_lifts.append({"name": e.name, "lift_type": e.lift_type})

    return {
        "position": {
            "nearest_node": node.name,
            "lon": node.lon,
            "lat": node.lat,
            "elev": node.elev,
            "snap_distance_m": round(dist_m, 1),
            "villages": node.villages if node.villages else None,
        },
        "adjacent": {
            "runs": adjacent_runs,
            "lifts": adjacent_lifts,
        },
        "reachable_villages": reachable,
    }


@app.get("/runs")
def runs(
    search: str | None = Query(default=None, description="Filter runs by name (case-insensitive)"),
    difficulty: str | None = Query(default=None, description="Filter by difficulty (e.g. 'easy', 'intermediate')"),
):
    """List all runs in the resort, optionally filtered."""
    results = []
    seen = set()
    for idx in graph._run_edges:
        e = graph.edges[idx]
        if not e.name or e.name in seen:
            continue
        if search and search.lower() not in e.name.lower():
            continue
        if difficulty and e.difficulty != difficulty:
            continue
        seen.add(e.name)
        from_node = graph.nodes[e.from_id]
        to_node = graph.nodes[e.to_id]
        results.append({
            "name": e.name,
            "difficulty": e.difficulty,
            "colour": DIFFICULTY_COLOUR.get(e.difficulty, ""),
            "from": {"name": from_node.name, "elev": from_node.elev},
            "to": {"name": to_node.name, "elev": to_node.elev},
            "length_m": round(e.length_m, 1),
            "elev_drop": round(e.elev_drop, 1),
        })
    results.sort(key=lambda x: x["name"])
    return {"count": len(results), "runs": results}


@app.get("/lifts")
def lifts(
    search: str | None = Query(default=None, description="Filter lifts by name (case-insensitive)"),
    lift_type: str | None = Query(default=None, description="Filter by type (e.g. 'gondola', 'chair_lift')"),
):
    """List all lifts in the resort, optionally filtered."""
    results = []
    seen = set()
    for idx in graph._lift_edges:
        e = graph.edges[idx]
        if not e.name or e.name in seen:
            continue
        if e.type == "lift_down":
            continue
        if search and search.lower() not in e.name.lower():
            continue
        if lift_type and e.lift_type != lift_type:
            continue
        seen.add(e.name)
        from_node = graph.nodes[e.from_id]
        to_node = graph.nodes[e.to_id]
        results.append({
            "name": e.name,
            "lift_type": e.lift_type,
            "from": {"name": from_node.name, "elev": from_node.elev},
            "to": {"name": to_node.name, "elev": to_node.elev},
            "length_m": round(e.length_m, 1),
            "elev_gain": round(to_node.elev - from_node.elev, 1),
        })
    results.sort(key=lambda x: x["name"])
    return {"count": len(results), "lifts": results}


@app.get("/graph")
def graph_data():
    """Return the raw routing graph (nodes and edges) for custom clients."""
    return {
        "nodes": [
            {
                "id": n.id, "lon": n.lon, "lat": n.lat,
                "elev": n.elev, "name": n.name, "villages": n.villages,
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "from": e.from_id, "to": e.to_id, "type": e.type,
                "name": e.name, "cost_base": e.cost_base,
                "length_m": round(e.length_m, 1),
                "elev_drop": round(e.elev_drop, 1),
                "difficulty": e.difficulty or None,
                "lift_type": e.lift_type or None,
                "geometry": e.geometry,
            }
            for e in graph.edges
        ],
        "villages": graph.villages,
    }
