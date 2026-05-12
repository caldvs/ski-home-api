"""Contraction Hierarchies — preprocessing + fast queries.

Build a hierarchical version of a routing graph by ranking nodes by
"importance" and replacing each node with shortcut edges between its
neighbours that preserve shortest paths. The augmented graph supports
**bidirectional queries that only explore upward edges**, which on a
geographic graph is dramatically smaller than the original.

For small graphs (≤ a few thousand nodes) the speedup is modest because
plain Dijkstra is already fast. For graphs with 10k+ nodes, CH typically
gives 100-1000× faster queries at the cost of a one-time preprocessing
step.

The algorithm here is the standard "contract by edge difference" approach
described in Geisberger et al. (2008). To keep the code readable we use
**initial-order contraction** — compute edge difference once for every
node, sort, then contract in that order. Production CH uses lazy
priority-queue updates; this is simpler and still works for our scale.
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from skiroute.graph import Edge, Graph


CostFn = Callable[[Edge], float]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CHEdge:
    """An edge in the CH-augmented graph.

    `is_shortcut=False` → wraps an original Edge.
    `is_shortcut=True`  → represents `shortcut_first` followed by `shortcut_second`
                          (both are indices into `CHGraph.edges`). Unpack
                          recursively to recover the underlying original edges.
    """
    from_id: int
    to_id: int
    cost: float
    is_shortcut: bool = False
    underlying: Edge | None = None
    shortcut_first: int = -1
    shortcut_second: int = -1
    via_node: int = -1


@dataclass
class CHGraph:
    """A graph augmented with contraction-hierarchy shortcuts and ordering.

    Build with `preprocess(graph, cost_fn)`. Query with `query(ch, src, tgt)`.
    """
    base: Graph
    edges: list[CHEdge] = field(default_factory=list)
    # node_id → rank (lower = contracted earlier, less important)
    order: dict[int, int] = field(default_factory=dict)
    # node_id → list of edge indices outgoing from this node
    adj_out: dict[int, list[int]] = field(default_factory=dict)
    # node_id → list of edge indices incoming to this node
    adj_in: dict[int, list[int]] = field(default_factory=dict)
    # Diagnostic counters
    preprocess_seconds: float = 0.0

    @property
    def n_shortcuts(self) -> int:
        return sum(1 for e in self.edges if e.is_shortcut)

    @property
    def n_original(self) -> int:
        return len(self.edges) - self.n_shortcuts


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


def preprocess(
    graph: Graph,
    cost_fn: CostFn,
    *,
    witness_hop_limit: int = 5,
    verbose: bool = False,
) -> CHGraph:
    """Build a CHGraph from a base graph + cost function.

    `witness_hop_limit` caps the depth of the witness search that decides
    whether a shortcut is needed. Lower = faster preprocessing but more
    spurious shortcuts. 5 is a reasonable default.
    """
    t0 = time.perf_counter()
    ch = CHGraph(base=graph)

    # 1. Initialise CH edges from the base graph
    for orig_edge in graph.edges:
        idx = len(ch.edges)
        ch.edges.append(CHEdge(
            from_id=orig_edge.from_id, to_id=orig_edge.to_id,
            cost=cost_fn(orig_edge),
            underlying=orig_edge,
        ))
        ch.adj_out.setdefault(orig_edge.from_id, []).append(idx)
        ch.adj_in.setdefault(orig_edge.to_id, []).append(idx)

    # 2. Compute initial edge difference for every node
    if verbose:
        print(f"  CH: computing initial node ordering for {len(graph.nodes)} nodes")

    contracted: set[int] = set()
    pending: list[tuple[int, int]] = []  # (edge_difference, node_id)
    for nid in graph.nodes:
        ed = _edge_difference(nid, ch, contracted, witness_hop_limit)
        pending.append((ed, nid))
    pending.sort()

    # 3. Contract in that order. For lazy-update CH, we'd recheck the
    # edge difference on pop and re-insert if it changed; here we use the
    # fixed initial order, which is simpler and works well at this scale.
    if verbose:
        print(f"  CH: contracting...")
    next_order = 0
    shortcuts_added = 0
    for _ed, nid in pending:
        ch.order[nid] = next_order
        next_order += 1
        contracted.add(nid)

        shortcuts_added += _insert_shortcuts(nid, ch, contracted, witness_hop_limit)

        if verbose and next_order % 100 == 0:
            print(f"    contracted {next_order}/{len(graph.nodes)}, "
                  f"{shortcuts_added} shortcuts so far")

    ch.preprocess_seconds = time.perf_counter() - t0
    if verbose:
        print(f"  CH: done in {ch.preprocess_seconds:.2f}s — "
              f"{len(graph.nodes)} nodes, {ch.n_shortcuts} shortcuts "
              f"(originals: {ch.n_original})")
    return ch


def _live_neighbors(
    nid: int, ch: CHGraph, contracted: set[int],
    direction: str,
) -> list[tuple[int, int]]:
    """Return (neighbour_node_id, edge_index) for non-contracted neighbours."""
    if direction == "out":
        adj = ch.adj_out.get(nid, [])
        nbr_key = "to_id"
    else:
        adj = ch.adj_in.get(nid, [])
        nbr_key = "from_id"
    result = []
    for ei in adj:
        e = ch.edges[ei]
        nbr = e.to_id if direction == "out" else e.from_id
        if nbr in contracted or nbr == nid:
            continue
        result.append((nbr, ei))
    return result


def _edge_difference(
    nid: int, ch: CHGraph, contracted: set[int], witness_hop_limit: int,
) -> int:
    """How many shortcuts would contracting `nid` add, minus how many
    incident edges it would remove? Lower is better."""
    in_neigh = _live_neighbors(nid, ch, contracted, "in")
    out_neigh = _live_neighbors(nid, ch, contracted, "out")
    shortcuts = 0
    for u, in_eidx in in_neigh:
        for w, out_eidx in out_neigh:
            if u == w:
                continue
            via_cost = ch.edges[in_eidx].cost + ch.edges[out_eidx].cost
            if not _has_witness(u, w, via_cost, nid, ch, contracted, witness_hop_limit):
                shortcuts += 1
    edges_removed = len(in_neigh) + len(out_neigh)
    return shortcuts - edges_removed


def _has_witness(
    u: int, w: int, max_cost: float, via: int,
    ch: CHGraph, contracted: set[int], hop_limit: int,
) -> bool:
    """Limited-Dijkstra witness search.

    Return True if there's a path u → ... → w of cost ≤ max_cost using
    only currently-alive nodes (not contracted, not `via`). Hops are
    limited to keep witness search cheap.
    """
    if u == w:
        return True
    dist: dict[int, float] = {u: 0.0}
    heap: list[tuple[float, int, int]] = [(0.0, u, 0)]
    while heap:
        d, n, h = heapq.heappop(heap)
        if d > dist.get(n, float("inf")):
            continue
        if n == w:
            return d <= max_cost
        if d > max_cost:
            return False
        if h >= hop_limit:
            continue
        for eidx in ch.adj_out.get(n, ()):
            e = ch.edges[eidx]
            nv = e.to_id
            if nv == via or nv in contracted:
                continue
            nd = d + e.cost
            if nd > max_cost:
                continue
            if nd < dist.get(nv, float("inf")):
                dist[nv] = nd
                heapq.heappush(heap, (nd, nv, h + 1))
    return False


def _insert_shortcuts(
    nid: int, ch: CHGraph, contracted: set[int], witness_hop_limit: int,
) -> int:
    """Add shortcuts for every (u, w) pair via nid where no witness exists.
    Returns number of shortcuts inserted."""
    # We need in_neigh and out_neigh BEFORE adding nid to contracted; the
    # caller has already added nid to `contracted`, but we need to treat
    # nid's edges as still alive for shortcut-decision purposes. Trick:
    # neighbours are filtered against `contracted`, and `contracted`
    # contains nid — but we filter `nbr == nid` separately, so we're fine.
    in_neigh = _live_neighbors(nid, ch, contracted, "in")
    out_neigh = _live_neighbors(nid, ch, contracted, "out")

    count = 0
    for u, in_eidx in in_neigh:
        for w, out_eidx in out_neigh:
            if u == w:
                continue
            via_cost = ch.edges[in_eidx].cost + ch.edges[out_eidx].cost
            if _has_witness(u, w, via_cost, nid, ch, contracted, witness_hop_limit):
                continue
            sc_idx = len(ch.edges)
            ch.edges.append(CHEdge(
                from_id=u, to_id=w, cost=via_cost,
                is_shortcut=True,
                via_node=nid,
                shortcut_first=in_eidx,
                shortcut_second=out_eidx,
            ))
            ch.adj_out.setdefault(u, []).append(sc_idx)
            ch.adj_in.setdefault(w, []).append(sc_idx)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@dataclass
class CHQueryStats:
    forward_visited: int = 0
    backward_visited: int = 0
    edges_relaxed: int = 0

    @property
    def nodes_visited(self) -> int:
        return self.forward_visited + self.backward_visited


def query(
    ch: CHGraph, src: int, tgt: int,
) -> tuple[Optional[list[Edge]], CHQueryStats]:
    """Bidirectional Dijkstra on the CH-augmented graph.

    Forward search from `src` traverses **upward** edges only (those
    ending at a higher-rank node). Backward search from `tgt` traverses
    edges that are upward in the *original* direction (those starting at
    a higher-rank node), going backwards. They meet at the highest-rank
    node on the shortest src→tgt path.

    Returns the unpacked sequence of original Edges plus stats.
    """
    stats = CHQueryStats()
    if src == tgt:
        return [], stats

    order = ch.order

    fwd_dist: dict[int, float] = {src: 0.0}
    fwd_prev_edge: dict[int, int] = {}
    fwd_heap: list[tuple[float, int]] = [(0.0, src)]
    fwd_settled: dict[int, float] = {}

    bwd_dist: dict[int, float] = {tgt: 0.0}
    bwd_prev_edge: dict[int, int] = {}
    bwd_heap: list[tuple[float, int]] = [(0.0, tgt)]
    bwd_settled: dict[int, float] = {}

    best_cost = float("inf")
    meeting_node: Optional[int] = None

    while fwd_heap or bwd_heap:
        f_top = fwd_heap[0][0] if fwd_heap else float("inf")
        b_top = bwd_heap[0][0] if bwd_heap else float("inf")
        # Standard CH termination: stop once *each* side's minimum
        # exceeds the current best meeting cost.
        if f_top >= best_cost and b_top >= best_cost:
            break

        expand_fwd = f_top <= b_top
        if expand_fwd and fwd_heap and f_top < best_cost:
            d, u = heapq.heappop(fwd_heap)
            if u in fwd_settled:
                continue
            fwd_settled[u] = d
            stats.forward_visited += 1

            if u in bwd_dist:
                total = d + bwd_dist[u]
                if total < best_cost:
                    best_cost = total
                    meeting_node = u

            # Relax outgoing edges that go to a HIGHER-rank node.
            order_u = order.get(u)
            for eidx in ch.adj_out.get(u, ()):
                e = ch.edges[eidx]
                v = e.to_id
                if order.get(v, -1) <= order_u:
                    continue
                if v in fwd_settled:
                    continue
                stats.edges_relaxed += 1
                nd = d + e.cost
                if nd < fwd_dist.get(v, float("inf")):
                    fwd_dist[v] = nd
                    fwd_prev_edge[v] = eidx
                    heapq.heappush(fwd_heap, (nd, v))
                    if v in bwd_dist:
                        total = nd + bwd_dist[v]
                        if total < best_cost:
                            best_cost = total
                            meeting_node = v
        elif bwd_heap and b_top < best_cost:
            d, u = heapq.heappop(bwd_heap)
            if u in bwd_settled:
                continue
            bwd_settled[u] = d
            stats.backward_visited += 1

            if u in fwd_dist:
                total = fwd_dist[u] + d
                if total < best_cost:
                    best_cost = total
                    meeting_node = u

            # Relax INCOMING edges in reverse, but only if the source
            # has higher order (= upward edge in original direction).
            order_u = order.get(u)
            for eidx in ch.adj_in.get(u, ()):
                e = ch.edges[eidx]
                v = e.from_id  # we walk backwards through e
                if order.get(v, -1) <= order_u:
                    continue
                if v in bwd_settled:
                    continue
                stats.edges_relaxed += 1
                nd = d + e.cost
                if nd < bwd_dist.get(v, float("inf")):
                    bwd_dist[v] = nd
                    bwd_prev_edge[v] = eidx
                    heapq.heappush(bwd_heap, (nd, v))
                    if v in fwd_dist:
                        total = fwd_dist[v] + nd
                        if total < best_cost:
                            best_cost = total
                            meeting_node = v
        else:
            break

    if meeting_node is None:
        return None, stats

    # Reconstruct CH path = forward portion (src → meeting) + backward
    # portion (meeting → tgt). The forward portion runs prev pointers
    # backwards; the backward portion runs to_id pointers forward.
    fwd_ch_edges: list[int] = []
    cur = meeting_node
    while cur in fwd_prev_edge:
        ei = fwd_prev_edge[cur]
        fwd_ch_edges.append(ei)
        cur = ch.edges[ei].from_id
    fwd_ch_edges.reverse()

    bwd_ch_edges: list[int] = []
    cur = meeting_node
    while cur in bwd_prev_edge:
        ei = bwd_prev_edge[cur]
        bwd_ch_edges.append(ei)
        cur = ch.edges[ei].to_id

    ch_path = fwd_ch_edges + bwd_ch_edges

    # Unpack any shortcuts into the original Edge sequence.
    out: list[Edge] = []
    for ch_eidx in ch_path:
        _unpack(ch_eidx, ch, out)
    return out, stats


def _unpack(ch_edge_idx: int, ch: CHGraph, out: list[Edge]) -> None:
    """Recursively expand a CH edge into its underlying original Edges."""
    e = ch.edges[ch_edge_idx]
    if not e.is_shortcut:
        if e.underlying is not None:
            out.append(e.underlying)
        return
    _unpack(e.shortcut_first, ch, out)
    _unpack(e.shortcut_second, ch, out)


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def route(
    ch: CHGraph, src: int, tgt: int,
) -> tuple[Optional[list[Edge]], CHQueryStats]:
    """Alias for `query`; mirrors the shape of the other algorithm entries."""
    return query(ch, src, tgt)
