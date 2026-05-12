"""Geometry helpers — distances, line lengths, GPKG parsing, spatial index.

Pure utilities. No package state, no I/O beyond raw bytes.
"""

from __future__ import annotations

import math
import struct
from collections import defaultdict


EARTH_RADIUS_M = 6_371_000


class SpatialGrid:
    """Lat/lon spatial hash for fast radius queries.

    Buckets points into square cells of approximately `cell_size_m` metres
    using an equirectangular projection anchored at a reference latitude.
    `query_within(lon, lat, r)` examines only nodes in cells overlapping
    the radius — typically a handful, not the whole graph.

    Used to make graph construction O(n) instead of O(n²) for node
    merging and gap-bridging on large fictional resorts.
    """

    __slots__ = ("cell_size_m", "_ref_lat", "_lon_scale", "_lat_scale",
                 "cells", "points")

    def __init__(self, cell_size_m: float = 250.0, ref_lat: float | None = None):
        self.cell_size_m = cell_size_m
        self._ref_lat = ref_lat
        self._lat_scale = 1.0 / (cell_size_m / 111_000.0)
        # _lon_scale set once ref_lat is known
        self._lon_scale: float | None = (
            None if ref_lat is None else self._compute_lon_scale(ref_lat)
        )
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.points: list[tuple[float, float]] = []  # parallel array (lon, lat)

    def _compute_lon_scale(self, ref_lat: float) -> float:
        meters_per_lon_deg = 111_000.0 * math.cos(math.radians(ref_lat))
        return 1.0 / (self.cell_size_m / meters_per_lon_deg)

    def _ensure_ref(self, lat: float) -> None:
        if self._ref_lat is None:
            self._ref_lat = lat
            self._lon_scale = self._compute_lon_scale(lat)

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        self._ensure_ref(lat)
        return (int(lat * self._lat_scale), int(lon * self._lon_scale))

    def add(self, lon: float, lat: float) -> int:
        """Add a point. Returns its index in `self.points`."""
        idx = len(self.points)
        self.points.append((lon, lat))
        self.cells[self._cell(lon, lat)].append(idx)
        return idx

    def __len__(self) -> int:
        return len(self.points)

    def query_within(
        self, lon: float, lat: float, radius_m: float
    ) -> list[tuple[int, float]]:
        """Return (point_index, distance_m) for every stored point within radius_m."""
        cy, cx = self._cell(lon, lat)
        n = max(1, int(radius_m / self.cell_size_m) + 1)
        results = []
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                bucket = self.cells.get((cy + dy, cx + dx))
                if not bucket:
                    continue
                for idx in bucket:
                    plon, plat = self.points[idx]
                    d = haversine(lon, lat, plon, plat)
                    if d <= radius_m:
                        results.append((idx, d))
        return results

    def query_pairs_within(self, radius_m: float):
        """Yield (i, j, distance) for every pair of points within radius_m, i<j.

        Used by gap bridging — O(n × k) where k is the average bucket density,
        instead of O(n²).
        """
        n = max(1, int(radius_m / self.cell_size_m) + 1)
        # Iterate over cells; for each point in cell, scan neighbour cells.
        for (cy, cx), bucket in self.cells.items():
            for i_idx, i in enumerate(bucket):
                ilon, ilat = self.points[i]
                # same cell: only j > i to avoid duplicates
                for j in bucket[i_idx + 1:]:
                    jlon, jlat = self.points[j]
                    d = haversine(ilon, ilat, jlon, jlat)
                    if d <= radius_m:
                        yield i, j, d
                # neighbour cells: only those greater in (dy, dx) to avoid dup
                for dy in range(-n, n + 1):
                    for dx in range(-n, n + 1):
                        if (dy, dx) <= (0, 0):
                            continue
                        nbr = self.cells.get((cy + dy, cx + dx))
                        if not nbr:
                            continue
                        for j in nbr:
                            jlon, jlat = self.points[j]
                            d = haversine(ilon, ilat, jlon, jlat)
                            if d <= radius_m:
                                yield i, j, d


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Distance in metres between two lon/lat points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def line_length(coords: list[tuple[float, float, float]]) -> float:
    """Total length of a (lon, lat, elev) polyline in metres."""
    total = 0.0
    for i in range(1, len(coords)):
        total += haversine(
            coords[i - 1][0], coords[i - 1][1],
            coords[i][0], coords[i][1],
        )
    return total


_ENVELOPE_SIZES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def parse_gpkg_linestring(blob: bytes | None) -> list[tuple[float, float, float]]:
    """Parse a GeoPackage linestring blob into [(lon, lat, elev), ...]."""
    if blob is None:
        return []
    flags = blob[3]
    envelope_indicator = (flags >> 1) & 0x07
    env_size = _ENVELOPE_SIZES.get(envelope_indicator, 0)
    wkb = blob[8 + env_size:]
    bo = wkb[0]
    endian = '<' if bo == 1 else '>'
    geom_type = struct.unpack(endian + 'I', wkb[1:5])[0]
    has_z = geom_type > 1000 or (geom_type & 0x80000000)
    num_points = struct.unpack(endian + 'I', wkb[5:9])[0]
    offset = 9
    dims = 3 if has_z else 2
    coords = []
    for _ in range(num_points):
        vals = struct.unpack(endian + f'{dims}d', wkb[offset:offset + 8 * dims])
        coords.append(vals)
        offset += 8 * dims
    if not has_z:
        coords = [(lon, lat, 0.0) for lon, lat in coords]
    return coords


def parse_gpkg_polygon(blob: bytes | None) -> list[list[list[float]]]:
    """Parse a GeoPackage Polygon/MultiPolygon into a list of rings.

    Each ring is [[lat, lon], ...] (Leaflet ordering).
    """
    if blob is None:
        return []
    flags = blob[3]
    envelope_indicator = (flags >> 1) & 0x07
    env_size = _ENVELOPE_SIZES.get(envelope_indicator, 0)
    wkb = blob[8 + env_size:]
    bo = wkb[0]
    endian = '<' if bo == 1 else '>'
    geom_type = struct.unpack(endian + 'I', wkb[1:5])[0]
    has_z = bool(geom_type & 0x80000000) or (geom_type // 1000 == 1)
    base_type = (geom_type & 0xFF) if (geom_type & 0x80000000) else (geom_type % 1000)

    offset = 5
    all_rings: list[list[list[float]]] = []

    def read_polygon(wkb: bytes, offset: int):
        num_rings = struct.unpack(endian + 'I', wkb[offset:offset + 4])[0]
        offset += 4
        rings = []
        for _ in range(num_rings):
            num_pts = struct.unpack(endian + 'I', wkb[offset:offset + 4])[0]
            offset += 4
            dims = 3 if has_z else 2
            ring = []
            for _ in range(num_pts):
                vals = struct.unpack(endian + f'{dims}d', wkb[offset:offset + 8 * dims])
                ring.append([vals[1], vals[0]])  # lat, lon
                offset += 8 * dims
            rings.append(ring)
        return rings, offset

    if base_type == 3:  # Polygon
        rings, _ = read_polygon(wkb, offset)
        all_rings.extend(rings)
    elif base_type == 6:  # MultiPolygon
        num_polys = struct.unpack(endian + 'I', wkb[offset:offset + 4])[0]
        offset += 4
        for _ in range(num_polys):
            offset += 5  # skip per-polygon byte order + type
            rings, offset = read_polygon(wkb, offset)
            all_rings.extend(rings)

    return all_rings
