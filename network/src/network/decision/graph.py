"""Network graph and path finding (Submodule 6.4).

Builds a weighted graph from the topology plus live signals and finds
paths for client/server traffic:

- Edge cost, Eq. (6):  c(l) = w_loss*loss% + w_lat*latency_ms + w_rel*(1 - rel),
  with rel = 1 - predicted failure probability. Down links are removed.
- ``shortest_path``: Dijkstra over switches/routers only (hosts and
  servers are endpoints, never transit), optionally avoiding links or
  nodes and requiring spare capacity for the demand (Eq. 5's constraint).
- ``demands``: the traffic the planner protects: every client <-> server
  pair and server <-> server pair, each ``demand_mbps``.

Paths are tuples of node names; links are canonical ``a-b`` names
(sorted endpoints), matching NetworkState and telemetry targets.
Standard library only (Dijkstra with heapq).
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

TRANSIT_KINDS = ("router", "switch")
ENDPOINT_KINDS = ("host", "server")
Path = Tuple[str, ...]


def link_name(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"{x}-{y}"


def path_links(path: Sequence[str]) -> List[str]:
    return [link_name(a, b) for a, b in zip(path, path[1:])]


@dataclass
class EdgeState:
    a: str
    b: str
    up: bool = True
    latency_ms: float = 0.0
    loss_pct: float = 0.0
    utilization_pct: float = 0.0
    capacity_mbps: float = 1000.0
    p_failure: float = 0.0

    @property
    def name(self) -> str:
        return link_name(self.a, self.b)

    @property
    def free_mbps(self) -> float:
        return self.capacity_mbps * max(0.0, 1.0 - self.utilization_pct / 100.0)


class NetworkGraph:
    def __init__(self, kinds: Dict[str, str], edges: Iterable[EdgeState]):
        self.kinds = dict(kinds)
        self.edges: Dict[str, EdgeState] = {e.name: e for e in edges}
        self._adj: Dict[str, List[str]] = {n: [] for n in self.kinds}
        for e in self.edges.values():
            self._adj[e.a].append(e.b)
            self._adj[e.b].append(e.a)

    @classmethod
    def from_topology(cls, topology) -> "NetworkGraph":
        kinds = {d.name: d.kind for d in topology.devices}
        return cls(kinds, [EdgeState(l.src, l.dst) for l in topology.links])

    def copy(self) -> "NetworkGraph":
        return NetworkGraph(self.kinds, [EdgeState(**e.__dict__) for e in self.edges.values()])

    # -- live signals ---------------------------------------------------------

    def apply_signals(self, health: Dict[str, dict], predictions: Dict[str, float]) -> "NetworkGraph":
        """Update edges from health reports (``link:<name>`` / ``node:<name>``) and predictions."""
        for edge in self.edges.values():
            report = health.get(f"link:{edge.name}")
            if report:
                m = report.get("metrics", {})
                edge.up = report.get("status") != "DOWN"
                edge.latency_ms = float(m.get("link.latency_ms", edge.latency_ms) or 0.0)
                edge.loss_pct = float(m.get("link.packet_loss_pct", edge.loss_pct) or 0.0)
                edge.utilization_pct = float(m.get("link.utilization_pct", edge.utilization_pct) or 0.0)
                edge.capacity_mbps = float(m.get("link.capacity_mbps", edge.capacity_mbps) or edge.capacity_mbps)
            for end in (edge.a, edge.b):
                node = health.get(f"node:{end}")
                if node and node.get("status") == "DOWN":
                    edge.up = False
            edge.p_failure = float(predictions.get(edge.name, 0.0))
        return self

    # -- queries ----------------------------------------------------------------

    def endpoints(self, name: str) -> Tuple[str, str]:
        e = self.edges[name]
        return e.a, e.b

    def links_of(self, node: str) -> List[str]:
        return sorted(link_name(node, n) for n in self._adj.get(node, []))

    def cost(self, edge: EdgeState, w_loss: float, w_lat: float, w_rel: float) -> float:
        """Eq. (6); a tiny per-hop term breaks ties in favour of shorter paths."""
        return w_loss * edge.loss_pct + w_lat * edge.latency_ms + w_rel * edge.p_failure + 1e-3

    def shortest_path(self, src: str, dst: str, weights: Tuple[float, float, float] = (1.0, 0.1, 10.0),
                      avoid_links: Set[str] = frozenset(), avoid_nodes: Set[str] = frozenset(),
                      min_free_mbps: float = 0.0,
                      edge_cost=None) -> Optional[Tuple[Path, float]]:
        """Cheapest usable path from ``src`` to ``dst`` (or None).

        ``edge_cost(edge)`` replaces the Eq. 6 cost (used by the random-reroute
        baseline, which gives every usable edge a random cost)."""
        if src not in self.kinds or dst not in self.kinds:
            return None
        counter = itertools.count()
        best = {src: 0.0}
        queue = [(0.0, next(counter), src, (src,))]
        while queue:
            cost, _, node, path = heapq.heappop(queue)
            if node == dst:
                return path, cost
            if cost > best.get(node, float("inf")):
                continue
            if node != src and self.kinds[node] not in TRANSIT_KINDS:
                continue  # endpoints are never transit
            for nxt in self._adj[node]:
                edge = self.edges[link_name(node, nxt)]
                if (not edge.up or edge.name in avoid_links or nxt in avoid_nodes
                        or edge.free_mbps < min_free_mbps):
                    continue
                if nxt != dst and self.kinds[nxt] not in TRANSIT_KINDS:
                    continue
                step = edge_cost(edge) if edge_cost is not None else self.cost(edge, *weights)
                new_cost = cost + step
                if new_cost < best.get(nxt, float("inf")):
                    best[nxt] = new_cost
                    heapq.heappush(queue, (new_cost, next(counter), nxt, path + (nxt,)))
        return None

    def demands(self) -> List[Tuple[str, str]]:
        """Client <-> server and server <-> server pairs."""
        servers = sorted(n for n, k in self.kinds.items() if k == "server")
        hosts = sorted(n for n, k in self.kinds.items() if k == "host")
        pairs = [(h, s) for h in hosts for s in servers]
        pairs += [(a, b) for a, b in itertools.combinations(servers, 2)]
        return pairs

    def baseline_routes(self) -> Dict[Tuple[str, str], Path]:
        """Routes on the healthy network (cheapest by hop count): the paths
        traffic is assumed to use before any decision changes them."""
        healthy = NetworkGraph(self.kinds, [EdgeState(e.a, e.b) for e in self.edges.values()])
        routes = {}
        for src, dst in self.demands():
            found = healthy.shortest_path(src, dst, (0.0, 0.0, 0.0))
            if found:
                routes[(src, dst)] = found[0]
        return routes

    def projected_utilization(self, routes: Dict[Tuple[str, str], Path], demand_mbps: float) -> Dict[str, float]:
        """Utilization % per link if each route carries ``demand_mbps`` on top
        of the measured utilization of the links it does not already use."""
        load = {name: e.utilization_pct * e.capacity_mbps / 100.0 for name, e in self.edges.items()}
        for path in routes.values():
            for name in path_links(path):
                load[name] = load.get(name, 0.0) + demand_mbps
        return {name: 100.0 * load[name] / max(self.edges[name].capacity_mbps, 1e-9) for name in load}
