"""Stitch two or more skiroute.Graphs into a single mega-resort graph.

The natural way to build a fictional mega-resort is *not* procedural —
it's stitching together real resort data. OpenSkiData covers every
piste and lift in the world; we use `skiroute.build_graph` for each
resort, then `stitch_graphs` here to merge them with a synthetic
connector (typically a long cable car or alpine traverse).

Example:

    tignes = skiroute.build_graph(gpkg, resort=ResortFilter(
        ski_area_pattern="%Tignes - Val%"
    ), destinations=tignes_villages)

    three_valleys = skiroute.build_graph(gpkg, resort=ResortFilter(
        ski_area_pattern="%Trois Vallées%"
    ), destinations=three_valleys_villages)

    mega = stitch_graphs(
        [tignes, three_valleys],
        connectors=[
            Connector(from_node_name="Aiguille Percée top",
                      to_node_name="Mont Vallon",
                      kind="cable_car", name="Vanoise Express"),
        ],
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import skiroute
from skiroute.geometry import haversine


@dataclass
class Connector:
    """A synthetic inter-resort link.

    `kind="cable_car"`: a long aerial transport. Riders ascend (and may
    descend) between the two stations. Cost is purely based on distance
    and a fixed ride duration.

    `kind="traverse"`: a long off-piste ski/skin traverse. One-directional
    if the destination is lower; bidirectional if same elevation.

    `kind="piste"`: a one-way downhill ski itinerary (e.g. an off-piste
    connector between adjacent resorts). Always directed higher → lower.
    """
    from_node_name: str
    to_node_name: str
    kind: str = "cable_car"          # "cable_car" | "traverse" | "piste"
    name: str = "Connector"
    # If endpoint-by-name lookup is ambiguous, use these to disambiguate:
    from_resort_index: Optional[int] = None
    to_resort_index: Optional[int] = None
    # Override cost (seconds). If None, derived from kind + distance.
    cost_override: Optional[float] = None
    # For cable_car kind: should it also have a down-direction edge?
    cable_car_down: bool = True


def auto_piste_connectors(
    graphs: list[skiroute.Graph],
    resort_pairs: list[tuple[int, int]],
    per_pair: int = 5,
    max_distance_m: float = 5500.0,
    min_drop_m: float = 200.0,
    high_min_elev_m: float = 2200.0,
    low_max_elev_m: float = 2400.0,
    name_prefix: str = "Off-piste itinerary",
) -> list[Connector]:
    """For each (resort_a, resort_b) pair, generate N piste-type connectors
    between high stations in A and low stations in B (and vice versa).

    Each connector is a one-way downhill piste from a high lift top to a
    lower lift bottom in the adjacent resort. Many of these per resort
    pair => many possible inter-resort routes, none individually optimal.
    """
    connectors: list[Connector] = []
    seen_pairs: set[tuple[int, int]] = set()

    for ri_a, ri_b in resort_pairs:
        g_a, g_b = graphs[ri_a], graphs[ri_b]

        # Collect "high" and "low" candidate nodes in each resort
        def by_role(g):
            highs = [n for n in g.nodes.values()
                     if n.elev >= high_min_elev_m and n.name]
            lows = [n for n in g.nodes.values()
                    if n.elev <= low_max_elev_m and n.name]
            return highs, lows

        highs_a, lows_a = by_role(g_a)
        highs_b, lows_b = by_role(g_b)

        # A→B: high in A → low in B; B→A: high in B → low in A.
        # Sort later by distance (shortest first), take top N.
        candidates: list[tuple[float, str, str, int, int]] = []
        for src_list, dst_list, src_idx, dst_idx in (
            (highs_a, lows_b, ri_a, ri_b),
            (highs_b, lows_a, ri_b, ri_a),
        ):
            for h in src_list:
                for l in dst_list:
                    if h.elev - l.elev < min_drop_m:
                        continue
                    d = haversine(h.lon, h.lat, l.lon, l.lat)
                    if d > max_distance_m:
                        continue
                    # Skiable? Gradient should be in a plausible piste range.
                    gradient = (h.elev - l.elev) / d
                    if gradient < 0.10 or gradient > 0.45:
                        continue
                    candidates.append((d, h.name, l.name, src_idx, dst_idx))

        # Deduplicate by (src_name, dst_name); keep shortest distance per pair.
        best: dict[tuple[str, str], tuple[float, int, int]] = {}
        for d, h_name, l_name, src_idx, dst_idx in candidates:
            key = (h_name, l_name)
            if key not in best or best[key][0] > d:
                best[key] = (d, src_idx, dst_idx)

        # Sort by distance, take top per_pair.
        chosen = sorted(
            ((k, v) for k, v in best.items()),
            key=lambda kv: kv[1][0],
        )[:per_pair]

        for (h_name, l_name), (d, src_idx, dst_idx) in chosen:
            key = (h_name + "/" + l_name, src_idx, dst_idx)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            connectors.append(Connector(
                from_node_name=h_name,
                to_node_name=l_name,
                kind="piste",
                name=f"{name_prefix}: {h_name} → {l_name}",
                from_resort_index=src_idx,
                to_resort_index=dst_idx,
            ))

    return connectors


def stitch_graphs(
    graphs: list[skiroute.Graph],
    connectors: list[Connector],
    verbose: bool = True,
) -> skiroute.Graph:
    """Merge multiple graphs and add synthetic connector edges between them.

    Node IDs are remapped so each input graph occupies a disjoint range.
    Destinations are merged (each resort's destinations carry through).
    Connectors are added as `lift` (cable_car) or `skate` (traverse) edges.
    """
    merged = skiroute.Graph()
    # Per-graph offsets — node id N in graphs[i] maps to N + offsets[i]
    offsets: list[int] = []
    # Per-graph name index for connector resolution
    name_indexes: list[dict[str, list[int]]] = []

    next_id = 0
    for gi, g in enumerate(graphs):
        offset = next_id
        offsets.append(offset)
        max_id = -1
        # Copy nodes with shifted IDs
        for nid, node in g.nodes.items():
            new_id = nid + offset
            new_node = skiroute.Node(
                id=new_id, lon=node.lon, lat=node.lat, elev=node.elev,
                name=node.name, destinations=list(node.destinations),
            )
            merged.nodes[new_id] = new_node
            max_id = max(max_id, nid)

        # Copy edges with shifted IDs
        for edge in g.edges:
            new_edge = skiroute.Edge(
                from_id=edge.from_id + offset,
                to_id=edge.to_id + offset,
                type=edge.type, name=edge.name,
                cost_base=edge.cost_base,
                length_m=edge.length_m, elev_drop=edge.elev_drop,
                difficulty=edge.difficulty, lift_type=edge.lift_type,
                geometry=list(edge.geometry),
            )
            idx = len(merged.edges)
            merged.edges.append(new_edge)
            merged.adjacency[new_edge.from_id].append(idx)

        # Carry destinations through (assume unique names across resorts)
        for dest_name, dest_info in g.destinations.items():
            if dest_name in merged.destinations:
                continue
            merged.destinations[dest_name] = dest_info

        # Index by name for connector resolution
        idx: dict[str, list[int]] = {}
        for nid, n in g.nodes.items():
            if not n.name:
                continue
            idx.setdefault(n.name.lower(), []).append(nid + offset)
        name_indexes.append(idx)

        next_id = offset + (max_id + 1)
        if verbose:
            print(f"  resort {gi}: {len(g.nodes)} nodes, {len(g.edges)} edges, "
                  f"offset {offset}, {len(g.destinations)} destinations")

    # Add connectors
    added = 0
    for c in connectors:
        from_id = _resolve_node(c.from_node_name, c.from_resort_index,
                                 name_indexes, merged, verbose)
        to_id = _resolve_node(c.to_node_name, c.to_resort_index,
                               name_indexes, merged, verbose)
        if from_id is None or to_id is None:
            if verbose:
                print(f"  SKIP connector {c.name}: endpoints not found")
            continue
        _add_connector(merged, from_id, to_id, c, verbose)
        added += 1

    if verbose:
        print(f"\nStitched graph: {len(merged.nodes)} nodes, "
              f"{len(merged.edges)} edges, "
              f"{len(merged.destinations)} destinations, "
              f"{added} connectors")
    return merged


def _resolve_node(
    name_substr: str, resort_index: Optional[int],
    name_indexes: list[dict[str, list[int]]],
    graph: skiroute.Graph, verbose: bool,
) -> Optional[int]:
    """Find a node id matching `name_substr` (substring, case-insensitive).
    If resort_index is given, only search that resort's nodes."""
    needle = name_substr.lower()
    candidates: list[int] = []
    sources = ([resort_index] if resort_index is not None
               else list(range(len(name_indexes))))
    for ri in sources:
        for nname, nids in name_indexes[ri].items():
            if needle in nname:
                candidates.extend(nids)

    if not candidates:
        if verbose:
            print(f"    no node matched '{name_substr}'")
        return None
    # When multiple matches: prefer the highest-elevation one — cable cars
    # typically go peak-to-peak, and "top" stations are what users mean.
    candidates.sort(key=lambda nid: -graph.nodes[nid].elev)
    if len(candidates) > 1 and verbose:
        chosen = graph.nodes[candidates[0]]
        print(f"    '{name_substr}' → {len(candidates)} matches; "
              f"picked highest: '{chosen.name}' at {chosen.elev:.0f}m")
    return candidates[0]


def _add_connector(
    graph: skiroute.Graph, from_id: int, to_id: int,
    c: Connector, verbose: bool,
) -> None:
    a = graph.nodes[from_id]
    b = graph.nodes[to_id]
    dist = haversine(a.lon, a.lat, b.lon, b.lat)
    elev_diff = b.elev - a.elev  # positive = a is lower

    if c.kind == "cable_car":
        # Realistic long aerial cable car: ~5 m/s plus ~10 min total dwell
        # (queue + loading + unloading at both ends). A 30 km cable car
        # takes 100+ minutes — not the "magic teleport" the prior cost
        # made it look like.
        ride_seconds = c.cost_override if c.cost_override is not None else (dist / 5.0 + 600)
        # Build UP edge if b is higher, otherwise the "up" is from b to a.
        if elev_diff >= 0:
            bottom_id, top_id, bot, top = from_id, to_id, a, b
        else:
            bottom_id, top_id, bot, top = to_id, from_id, b, a
        up = skiroute.Edge(
            from_id=bottom_id, to_id=top_id, type="lift",
            name=c.name, lift_type="cable_car",
            length_m=dist, elev_drop=-(top.elev - bot.elev),
            cost_base=ride_seconds,
        )
        up.geometry = [(bot.lon, bot.lat), (top.lon, top.lat)]
        _push_edge(graph, up)
        if c.cable_car_down:
            down = skiroute.Edge(
                from_id=top_id, to_id=bottom_id, type="lift_down",
                name=f"{c.name} (down)", lift_type="cable_car",
                length_m=dist, elev_drop=(top.elev - bot.elev),
                cost_base=ride_seconds + 600,
            )
            down.geometry = [(top.lon, top.lat), (bot.lon, bot.lat)]
            _push_edge(graph, down)
        if verbose:
            print(f"  {c.name}: cable car {dist:.0f}m, "
                  f"{abs(top.elev - bot.elev):.0f}m vert, "
                  f"{ride_seconds:.0f}s ride")
    elif c.kind == "traverse" or c.kind == "piste":
        # Off-piste itinerary or freeride piste — one-way, higher → lower.
        # Cost: ~4 m/s effective speed + transition overhead.
        # Difficulty: 'freeride' for traverses (no grooming, often through
        # the Vanoise NP), 'advanced' for ski itineraries on the resort edge.
        if elev_diff >= 0:
            src, dst, srcN, dstN = to_id, from_id, b, a
        else:
            src, dst, srcN, dstN = from_id, to_id, a, b
        drop = srcN.elev - dstN.elev
        cost = c.cost_override if c.cost_override is not None else (dist / 4.0 + 400)
        difficulty = "advanced" if c.kind == "piste" else "freeride"
        edge = skiroute.Edge(
            from_id=src, to_id=dst, type="run",
            name=c.name, difficulty=difficulty,
            length_m=dist, elev_drop=drop, cost_base=cost,
        )
        edge.geometry = [(srcN.lon, srcN.lat), (dstN.lon, dstN.lat)]
        _push_edge(graph, edge)
        if verbose:
            print(f"  {c.name}: {c.kind} {dist:.0f}m, "
                  f"{drop:.0f}m drop, {cost:.0f}s")
    else:
        raise ValueError(f"Unknown connector kind: {c.kind!r}")


def _push_edge(graph: skiroute.Graph, edge: skiroute.Edge) -> None:
    idx = len(graph.edges)
    graph.edges.append(edge)
    graph.adjacency[edge.from_id].append(idx)
    graph._invalidate_cache()
