"""Traffic-delivery model (Submodule 7.2).

Keeps the routes actually *installed* in the (simulated) data plane, one
per client/server demand, and each step works out how much of every
demand gets through:

    delivered = 0                       if any node or link on its route is
                                        not physically working
    delivered = prod(1 - loss_l / 100)  otherwise, using measured loss

This is the "user-visible" view: it lets self-healing check that a reroute
really carries traffic, and lets the evaluation measure the packet loss
users see during recovery. Link *load* telemetry stays synthetic (Module 2)
and does not follow these routes.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Tuple

from network.decision.graph import NetworkGraph, Path, path_links
from network.telemetry.metrics import Metric

Pair = Tuple[str, str]


class ServiceModel:
    def __init__(self, graph: NetworkGraph, physical: Callable[[], object], store=None):
        self.graph = graph
        self._physical = physical
        self.store = store
        self.baseline: Dict[Pair, Path] = graph.baseline_routes()
        self.routes: Dict[Pair, Path] = dict(self.baseline)
        self.history: Dict[Pair, List[Tuple[float, float]]] = {p: [] for p in self.routes}

    def _loss(self, link: str) -> float:
        if self.store is None:
            return 0.0
        value = self.store.latest_value(Metric.LINK_PACKET_LOSS_PCT, link)
        return 0.0 if value is None else max(0.0, min(100.0, value))

    def delivered(self, pair: Pair) -> float:
        path = self.routes[pair]
        net = self._physical()
        if net is None:
            return 0.0
        if not all(net.node_physically_up(n) for n in path):
            return 0.0
        fraction = 1.0
        for a, b in zip(path, path[1:]):
            if not net.link_physically_up(a, b):
                return 0.0
        for link in path_links(path):
            fraction *= 1.0 - self._loss(link) / 100.0
        return fraction

    def step(self, ts: float) -> Dict[Pair, float]:
        out = {pair: self.delivered(pair) for pair in self.routes}
        for pair, value in out.items():
            self.history[pair].append((ts, value))
        return out

    def latest(self, pair: Pair) -> Optional[float]:
        series = self.history.get(pair)
        return series[-1][1] if series else None

    def demands_using(self, link: Optional[str] = None, node: Optional[str] = None,
                      routes: Optional[Dict[Pair, Path]] = None) -> List[Pair]:
        routes = routes if routes is not None else self.baseline
        out = []
        for pair, path in routes.items():
            if (link and link in path_links(path)) or (node and node in path):
                out.append(pair)
        return sorted(out)

    def lost_fraction(self, pairs: Iterable[Pair], start: float, end: float) -> Optional[float]:
        values = [1.0 - v for p in pairs for ts, v in self.history.get(p, []) if start <= ts <= end]
        return sum(values) / len(values) if values else None

    def restored_at(self, pairs: Iterable[Pair], start: float, end: float, ok: float = 0.9) -> Optional[float]:
        """First time in [start, end] from which every pair is delivered >= ``ok``
        for the rest of the window (None if never)."""
        pairs = list(pairs)
        times = sorted({ts for p in pairs for ts, _ in self.history.get(p, []) if start <= ts <= end})
        good_from = None
        for ts in times:
            all_ok = all(dict(self.history[p]).get(ts, 0.0) >= ok for p in pairs)
            if all_ok and good_from is None:
                good_from = ts
            elif not all_ok:
                good_from = None
        return good_from
