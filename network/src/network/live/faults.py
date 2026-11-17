"""Physical faults on the live network (Module 7 fault model, live).

A *physical* fault breaks the data plane without telling the controller:
the link keeps its carrier and stays "connected" in ``NetworkState``, but
every packet is dropped (``tc netem loss 100%`` on both interfaces), like a
cut fibre behind a media converter. Only measurement (link probes) can see
it. Repairing restores whatever impairments the controller had configured.

Mirrors :class:`network.simulation.dataplane.SimulatedNet`'s
``break_link`` / ``repair_link`` / ``break_node`` / ``repair_node`` and
``link_physically_up`` / ``node_physically_up``.
"""

from __future__ import annotations

from typing import Set, Tuple


def _key(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted((a, b)))


class LiveFaults:
    def __init__(self, controller):
        self.controller = controller
        self.broken_links: Set[Tuple[str, str]] = set()
        self.broken_nodes: Set[str] = set()
        controller.physical_faults = self  # impairment changes keep faults in place

    @property
    def _net(self):
        return self.controller._net

    def _links(self, a: str, b: str):
        return self._net.linksBetween(self._net.get(a), self._net.get(b))

    def _drop_all(self, a: str, b: str) -> None:
        for link in self._links(a, b):
            for intf in (link.intf1, link.intf2):
                intf.tc("%s qdisc replace dev %s root netem loss 100%%")

    def _restore(self, a: str, b: str) -> None:
        if _key(a, b) in self.broken_links or any(n in self.broken_nodes for n in (a, b)):
            return  # still broken for another reason
        key = "-".join(_key(a, b))
        self.controller._apply_impairment(key, self.controller.impairments().get(key, {}))

    def _node_links(self, node: str):
        return [(l.src, l.dst) for l in self.controller.config.links if node in (l.src, l.dst)]

    def break_link(self, a: str, b: str) -> None:
        self.broken_links.add(_key(a, b))
        self._drop_all(a, b)

    def repair_link(self, a: str, b: str) -> None:
        self.broken_links.discard(_key(a, b))
        self._restore(a, b)

    def break_node(self, node: str) -> None:
        self.broken_nodes.add(node)
        for a, b in self._node_links(node):
            self._drop_all(a, b)

    def repair_node(self, node: str) -> None:
        self.broken_nodes.discard(node)
        for a, b in self._node_links(node):
            self._restore(a, b)

    def link_physically_up(self, a: str, b: str) -> bool:
        return _key(a, b) not in self.broken_links and not ({a, b} & self.broken_nodes)

    def node_physically_up(self, node: str) -> bool:
        return node not in self.broken_nodes
