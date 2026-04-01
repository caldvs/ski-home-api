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
            ))
            self.adjacency.setdefault(e["from"], []).append(idx)

        self.villages = data.get("villages", {})

    def find_nearest_node(self, lon: float, lat: float) -> tuple[Node, float]:
        best, best_dist = None, float("inf")
        for node in self.nodes.values():
            d = haversine(lon, lat, node.lon, node.lat)
            if d < best_dist:
                best_dist = d
                best = node
        return best, best_dist

    def route(self, from_node_id: int, to_village: str, difficulty: str = "any-piste"):
        penalties = DIFFICULTY_PENALTY.get(difficulty, DIFFICULTY_PENALTY["any-piste"])

        target_nodes = {n.id for n in self.nodes.values() if to_village in n.villages}
        if not target_nodes:
            return None, f"No nodes found in village '{to_village}'"

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

                # Extend through village to reach the bottom
                cursor = u
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

                new_dist = d + cost
                if new_dist < dist[v]:
                    dist[v] = new_dist
                    prev[v] = u
                    prev_edge[v] = edge
                    heapq.heappush(heap, (new_dist, v))

        return None, "No route found (target village unreachable)"


def consolidate_legs(graph: SkiGraph, path: list[Edge]) -> list[dict]:
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
            legs.append(_build_leg(graph, current_edges, current_type, current_name))
            current_edges = [edge]
            current_type = edge.type
            current_name = edge.name

    legs.append(_build_leg(graph, current_edges, current_type, current_name))
    return legs


def _build_leg(graph: SkiGraph, edges: list[Edge], leg_type: str, name: str) -> dict:
    first, last = edges[0], edges[-1]
    from_node = graph.nodes[first.from_id]
    to_node = graph.nodes[last.to_id]

    leg = {
        "type": leg_type,
        "name": name,
        "from": {"name": from_node.name, "elev": from_node.elev},
        "to": {"name": to_node.name, "elev": to_node.elev},
        "length_m": round(sum(e.length_m for e in edges), 1),
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

    return leg


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
    lon: float = Query(description="Longitude of current position"),
    lat: float = Query(description="Latitude of current position"),
    village: str = Query(description="Destination village name"),
    difficulty: Difficulty = Query(
        default=Difficulty.any_piste,
        description="Difficulty preference",
    ),
):
    """Compute a route from GPS coordinates to a home village."""
    if village not in graph.villages:
        return {"error": f"Unknown village '{village}'", "villages": list(graph.villages.keys())}

    node, dist_m = graph.find_nearest_node(lon, lat)
    path, err = graph.route(node.id, village, difficulty.value)

    if err:
        return {"error": err}

    legs = consolidate_legs(graph, path)
    last_node = graph.nodes[path[-1].to_id] if path else node

    run_count = sum(1 for l in legs if l["type"] in ("run", "connection"))
    lift_count = sum(1 for l in legs if l["type"] in ("lift", "lift_down"))
    total_time_s = sum(e.cost_base for e in path)

    return {
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
        "difficulty": difficulty.value,
        "summary": {
            "runs": run_count,
            "lifts": lift_count,
            "estimated_minutes": round(total_time_s / 60),
        },
        "legs": legs,
    }
