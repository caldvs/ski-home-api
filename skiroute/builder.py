"""Build a routable Graph from an OpenSkiData GeoPackage.

Top-level entry point:

    skiroute.build_graph(gpkg_path, resort=ResortFilter(...), config=...)

Internals: a stateful _GraphBuilder that walks lifts → runs → gaps →
destinations → overrides in five steps, matching the proven Tignes
pipeline but parameterised so it works for any resort in OpenSkiData.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from skiroute.config import BuildConfig, Destination, ResortFilter
from skiroute.geometry import (
    SpatialGrid,
    haversine,
    line_length,
    parse_gpkg_linestring,
)
from skiroute.graph import Edge, Graph, Node
from skiroute.overrides import apply_overrides


def build_graph(
    gpkg_path: str | Path,
    resort: ResortFilter | None = None,
    config: BuildConfig | None = None,
    destinations: list[Destination] | None = None,
    overrides_path: str | Path | None = None,
    verbose: bool = True,
) -> Graph:
    """Build a complete graph for one resort from an OpenSkiData GPKG.

    Args:
        gpkg_path: Path to an OpenSkiData GeoPackage file.
        resort: Which slice of the GPKG to build. If None, builds the entire
            file (rarely what you want — OpenSkiData covers the world).
        config: Tunables for graph construction. Defaults are Tignes-tested.
        destinations: Optional destination clusters (e.g. base villages).
            Graph nodes inside each are tagged so `route_home()` can target them.
        overrides_path: Optional JSON file patching the graph after build.
        verbose: Print progress.

    Returns:
        A fully built Graph, ready for `route()` or `Graph.save()`.
    """
    resort = resort or ResortFilter()
    config = config or BuildConfig()

    builder = _GraphBuilder(gpkg_path, resort, config, verbose)
    try:
        builder.extract_lifts()
        builder.extract_runs()
        builder.build_skating_edges()
        if destinations:
            builder.tag_destinations(destinations)
        if overrides_path:
            apply_overrides(builder.graph, overrides_path, verbose=verbose)
        if verbose:
            print(f"Graph complete: {len(builder.graph.nodes)} nodes, "
                  f"{len(builder.graph.edges)} edges")
        return builder.graph
    finally:
        builder.conn.close()


# ---------------------------------------------------------------------------
# Internal builder
# ---------------------------------------------------------------------------


class _GraphBuilder:
    def __init__(
        self,
        gpkg_path: str | Path,
        resort: ResortFilter,
        config: BuildConfig,
        verbose: bool,
    ):
        self.gpkg_path = str(gpkg_path)
        self.resort = resort
        self.config = config
        self.verbose = verbose
        self.conn = sqlite3.connect(self.gpkg_path)

        self.graph = Graph()
        self._next_node_id = 0
        # Spatial hash for fast node-merge and gap-bridging queries.
        # Cell size matches the max gap distance so a 3×3 lookup is sufficient.
        self._grid = SpatialGrid(cell_size_m=max(
            config.max_gap_distance_m, config.merge_radius_m * 2, 100.0
        ))
        # Parallel array of elev for each grid-indexed point (grid stores lon/lat only).
        self._grid_elev: list[float] = []
        self._grid_node_id: list[int] = []

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # --- node management ---

    def _new_node_id(self) -> int:
        nid = self._next_node_id
        self._next_node_id += 1
        return nid

    def _find_or_create_node(
        self, lon: float, lat: float, elev: float,
        name: str = "", merge_radius: float | None = None,
    ) -> int:
        merge_r = merge_radius if merge_radius is not None else self.config.merge_radius_m
        merge_e = self.config.merge_elev_m
        for pt_idx, dist in self._grid.query_within(lon, lat, merge_r):
            if abs(elev - self._grid_elev[pt_idx]) < merge_e:
                sid = self._grid_node_id[pt_idx]
                if name and not self.graph.nodes[sid].name:
                    self.graph.nodes[sid].name = name
                return sid

        nid = self._new_node_id()
        self.graph.nodes[nid] = Node(nid, lon, lat, elev, name)
        idx = self._grid.add(lon, lat)
        self._grid_elev.append(elev)
        self._grid_node_id.append(nid)
        assert idx == len(self._grid_elev) - 1
        return nid

    def _add_edge(self, edge: Edge) -> None:
        idx = len(self.graph.edges)
        self.graph.edges.append(edge)
        self.graph.adjacency[edge.from_id].append(idx)

    # --- SQL filter construction ---

    def _resort_where(self, table_alias: str = "") -> tuple[str, list]:
        """Build a SQL WHERE clause + params from the ResortFilter."""
        prefix = f"{table_alias}." if table_alias else ""
        clauses = []
        params: list = []

        if self.resort.localities:
            locality_clauses = [
                f"{prefix}localities LIKE ?" for _ in self.resort.localities
            ]
            clauses.append("(" + " OR ".join(locality_clauses) + ")")
            params.extend(f"%{loc}%" for loc in self.resort.localities)

        if self.resort.ski_area_pattern:
            clauses.append(f"{prefix}ski_area_names LIKE ?")
            params.append(self.resort.ski_area_pattern)

        if not clauses:
            return "1=1", []
        return " AND ".join(clauses), params

    # --- step 1: lifts ---

    def extract_lifts(self) -> None:
        self._log("Step 1: Extracting lifts")
        where, params = self._resort_where()
        exclude_types = self.resort.exclude_lift_types
        exclude_placeholders = ",".join("?" * len(exclude_types))
        exclude_clause = (
            f"AND lift_type NOT IN ({exclude_placeholders})"
            if exclude_types else ""
        )

        sql = f"""
            SELECT id, name, lift_type, geometry, oneway
            FROM lifts_linestring
            WHERE {where}
            {exclude_clause}
        """
        cur = self.conn.cursor()
        cur.execute(sql, params + exclude_types)

        count = 0
        for row in cur.fetchall():
            db_id, name, lift_type, geom_blob, _oneway = row
            coords = parse_gpkg_linestring(geom_blob)
            if len(coords) < 2:
                continue

            bottom, top = coords[0], coords[-1]
            if bottom[2] > top[2]:
                bottom, top = top, bottom

            bottom_id = self._find_or_create_node(
                bottom[0], bottom[1], bottom[2],
                f"{name} bottom" if name else "")
            top_id = self._find_or_create_node(
                top[0], top[1], top[2],
                f"{name} top" if name else "")

            length = line_length(coords)
            elev_gain = top[2] - bottom[2]
            base_cost = self.config.lift_time_by_type.get(
                lift_type, self.config.default_lift_time)

            up = Edge(
                from_id=bottom_id, to_id=top_id, type="lift",
                name=name or f"Lift {db_id}",
                length_m=length, elev_drop=-elev_gain,
                lift_type=lift_type or "", cost_base=base_cost,
            )
            up.geometry = [(c[0], c[1]) for c in coords]
            bn, tn = self.graph.nodes[bottom_id], self.graph.nodes[top_id]
            up.geometry[0], up.geometry[-1] = (bn.lon, bn.lat), (tn.lon, tn.lat)
            self._add_edge(up)
            count += 1

            if (self.config.gondola_funicular_cablecar_can_descend
                    and lift_type in ("gondola", "funicular", "cable_car")):
                down = Edge(
                    from_id=top_id, to_id=bottom_id, type="lift_down",
                    name=f"{name or f'Lift {db_id}'} (down)",
                    length_m=length, elev_drop=elev_gain,
                    lift_type=lift_type or "",
                    cost_base=base_cost + self.config.lift_down_penalty,
                )
                down.geometry = [(c[0], c[1]) for c in reversed(coords)]
                down.geometry[0], down.geometry[-1] = (tn.lon, tn.lat), (bn.lon, bn.lat)
                self._add_edge(down)

        self._log(f"  Extracted {count} lifts")

    # --- step 2: runs ---

    def extract_runs(self) -> None:
        self._log("Step 2: Extracting runs")
        where, params = self._resort_where()
        sql = f"""
            SELECT id, name, difficulty, geometry, uses
            FROM runs_linestring
            WHERE {where}
              AND (uses LIKE '%downhill%' OR uses LIKE '%connection%')
              AND difficulty IS NOT NULL
        """
        cur = self.conn.cursor()
        cur.execute(sql, params)

        all_runs = []
        for row in cur.fetchall():
            db_id, name, difficulty, geom_blob, uses = row
            coords = parse_gpkg_linestring(geom_blob)
            if len(coords) < 2:
                continue
            elev_drop = coords[0][2] - coords[-1][2]
            is_connection = "connection" in (uses or "")
            if elev_drop < -5 and not is_connection:
                continue
            all_runs.append((db_id, name, difficulty, coords, uses, is_connection))

        # Pass 1: ensure every run endpoint exists as a node before splitting
        for db_id, name, difficulty, coords, uses, is_connection in all_runs:
            start, end = coords[0], coords[-1]
            self._find_or_create_node(start[0], start[1], start[2],
                                       f"{name} top" if name else "")
            self._find_or_create_node(end[0], end[1], end[2],
                                       f"{name} bottom" if name else "")

        # Pass 2: split runs at intermediate points near existing nodes
        count = 0
        for db_id, name, difficulty, coords, uses, is_connection in all_runs:
            edge_type = "connection" if is_connection else "run"
            run_name = name or f"Run {db_id}"

            split_indices = [0]
            for pt_idx in range(1, len(coords) - 1):
                pt = coords[pt_idx]
                for nearby_idx, _d in self._grid.query_within(
                    pt[0], pt[1], self.config.split_min_dist_m
                ):
                    if abs(pt[2] - self._grid_elev[nearby_idx]) < self.config.split_min_elev_m:
                        split_indices.append(pt_idx)
                        break
            split_indices.append(len(coords) - 1)
            split_indices = sorted(set(split_indices))

            cleaned = [split_indices[0]]
            for si in split_indices[1:]:
                if si - cleaned[-1] >= 1:
                    cleaned.append(si)
            split_indices = cleaned

            for seg_i in range(len(split_indices) - 1):
                seg_coords = coords[split_indices[seg_i]:split_indices[seg_i + 1] + 1]
                seg_start, seg_end = seg_coords[0], seg_coords[-1]

                seg_start_name = f"{name} top" if name and seg_i == 0 else ""
                seg_end_name = (
                    f"{name} bottom" if name and seg_i == len(split_indices) - 2 else ""
                )

                a = self._find_or_create_node(seg_start[0], seg_start[1], seg_start[2],
                                               seg_start_name)
                b = self._find_or_create_node(seg_end[0], seg_end[1], seg_end[2],
                                               seg_end_name)
                if a == b:
                    continue

                seg_length = line_length(seg_coords)
                seg_drop = seg_start[2] - seg_end[2]

                if seg_drop < 10 and not is_connection:
                    elev_gain = max(0, seg_end[2] - seg_start[2])
                    if elev_gain <= 5:
                        base_cost = self.config.skate_cost_flat
                    elif elev_gain <= 15:
                        base_cost = self.config.skate_cost_moderate
                    else:
                        base_cost = self.config.skate_cost_steep
                    seg_type = "skate"
                    seg_difficulty = ""
                else:
                    base_cost = seg_length / self.config.ski_speed_ms
                    if is_connection:
                        base_cost *= self.config.connection_cost_multiplier
                    seg_type = edge_type
                    seg_difficulty = difficulty

                edge = Edge(
                    from_id=a, to_id=b, type=seg_type, name=run_name,
                    difficulty=seg_difficulty, length_m=seg_length,
                    elev_drop=seg_drop, cost_base=base_cost,
                )
                edge.geometry = [(c[0], c[1]) for c in seg_coords]
                sn, en = self.graph.nodes[a], self.graph.nodes[b]
                edge.geometry[0], edge.geometry[-1] = (sn.lon, sn.lat), (en.lon, en.lat)
                self._add_edge(edge)
                count += 1

        self._log(f"  Extracted {count} runs/connections")

    # --- step 3: skating bridges ---

    def build_skating_edges(self) -> None:
        self._log("Step 3: Building skating edges")
        existing = {(e.from_id, e.to_id) for e in self.graph.edges}
        cfg = self.config
        count = skipped = 0

        def skate_cost(elev_gain: float, distance: float) -> float:
            if elev_gain <= 5:
                return cfg.skate_cost_flat + distance / 2
            if elev_gain <= 15:
                return cfg.skate_cost_moderate + distance / 1.5
            return cfg.skate_cost_steep + distance + elev_gain * 10

        # Iterate pairs within max_gap_distance via the spatial index — O(n·k)
        # instead of the previous O(n²).
        for pi, pj, dist in self._grid.query_pairs_within(cfg.max_gap_distance_m):
            a = self.graph.nodes[self._grid_node_id[pi]]
            b = self.graph.nodes[self._grid_node_id[pj]]
            if abs(b.elev - a.elev) > cfg.max_gap_elev_change_m:
                continue

            if (a.id, b.id) not in existing:
                gain = max(0, b.elev - a.elev)
                e = Edge(
                    from_id=a.id, to_id=b.id, type="skate",
                    name=f"Skate {dist:.0f}m",
                    length_m=dist, elev_drop=-gain,
                    cost_base=skate_cost(gain, dist),
                )
                e.geometry = [(a.lon, a.lat), (b.lon, b.lat)]
                self._add_edge(e)
                count += 1
            else:
                skipped += 1

            if (b.id, a.id) not in existing:
                gain = max(0, a.elev - b.elev)
                e = Edge(
                    from_id=b.id, to_id=a.id, type="skate",
                    name=f"Skate {dist:.0f}m",
                    length_m=dist, elev_drop=-gain,
                    cost_base=skate_cost(gain, dist),
                )
                e.geometry = [(b.lon, b.lat), (a.lon, a.lat)]
                self._add_edge(e)
                count += 1
            else:
                skipped += 1

        self._log(f"  Created {count} skating edges (skipped {skipped} existing)")

    # --- step 4: destinations ---

    def tag_destinations(self, destinations: list[Destination]) -> None:
        self._log("Step 4: Tagging destination nodes")
        for node in self.graph.nodes.values():
            for d in destinations:
                if haversine(node.lon, node.lat, d.lon, d.lat) > d.radius_m:
                    continue
                if d.elev is not None and (node.elev - d.elev) > d.elev_tolerance_m:
                    continue
                node.destinations.append(d.name)

        # Save destinations on the graph for downstream consumers
        self.graph.destinations = {
            d.name: {"lat": d.lat, "lon": d.lon, "elev": d.elev or 0.0}
            for d in destinations
        }

        if self.verbose:
            from collections import defaultdict
            counts = defaultdict(int)
            for node in self.graph.nodes.values():
                for name in node.destinations:
                    counts[name] += 1
            for name, c in sorted(counts.items()):
                self._log(f"  {name}: {c} nodes")
