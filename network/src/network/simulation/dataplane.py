"""In-memory stand-in for the Mininet data plane (Module 3).

Lets the real :class:`NetworkController` and :class:`FailureManager` run
without Mininet, Open vSwitch or root, so failure scenarios can be
executed and recorded on any machine. Nothing here forwards packets: it
only accepts the calls the controller makes, so telemetry for such runs
must come from :class:`SimulatedSource` and is always marked simulated.

``SimulatedNode.start`` accepts an optional ``controllers`` argument to
match the controller's current call, which differs from real Mininet
(``OVSSwitch.start(controllers)`` requires it). That mismatch is a known
Module 1 defect tracked for Module 2.0; this class does not hide it on a
real network.

Physical faults (Module 7): ``break_link`` / ``break_node`` model damage
that no software command can fix (a cut cable, dead switch hardware).
The controller may still *believe* such a link or node is up after
reconnecting it; ``link_physically_up`` / ``node_physically_up`` report
what is really working, and the simulated telemetry source reads those,
so a "successful" command that changed nothing is visible in telemetry.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from network.config import TopologyConfig, load_topology_config

DEFAULT_CONFIG_PATH = "config/enterprise.yaml"


class SimulatedNode:
    def __init__(self, name: str, kind: str):
        self.name = name
        self.kind = kind
        self.running = True

    def start(self, controllers=None) -> None:
        self.running = True

    def stop(self, deleteIntfs: bool = False) -> None:  # noqa: N803 - Mininet's name
        self.running = False


class SimulatedNet:
    """Implements the subset of ``mininet.net.Mininet`` the controller uses."""

    def __init__(self, nodes: List[SimulatedNode]):
        self._nodes: Dict[str, SimulatedNode] = {n.name: n for n in nodes}
        self.controllers: list = []
        self.link_status: Dict[tuple, str] = {}
        self.impairments: Dict[str, dict] = {}
        self.broken_links: set = set()
        self.broken_nodes: set = set()

    @property
    def hosts(self) -> List[SimulatedNode]:
        return [n for n in self._nodes.values() if n.kind in ("host", "server")]

    @property
    def switches(self) -> List[SimulatedNode]:
        return [n for n in self._nodes.values() if n.kind in ("router", "switch")]

    def get(self, name: str) -> SimulatedNode:
        return self._nodes[name]

    def configLinkStatus(self, src: str, dst: str, status: str) -> None:  # noqa: N802
        self.link_status[tuple(sorted((src, dst)))] = status

    def configLinkImpairment(self, key: str, params: dict) -> None:  # noqa: N802
        if params:
            self.impairments[key] = dict(params)
        else:
            self.impairments.pop(key, None)

    # -- physical faults (Module 7) -------------------------------------------

    def break_link(self, a: str, b: str) -> None:
        self.broken_links.add(tuple(sorted((a, b))))

    def repair_link(self, a: str, b: str) -> None:
        self.broken_links.discard(tuple(sorted((a, b))))

    def break_node(self, name: str) -> None:
        self.broken_nodes.add(name)

    def repair_node(self, name: str) -> None:
        self.broken_nodes.discard(name)

    def link_physically_up(self, a: str, b: str) -> bool:
        key = tuple(sorted((a, b)))
        return self.link_status.get(key, "up") != "down" and key not in self.broken_links

    def node_physically_up(self, name: str) -> bool:
        node = self._nodes.get(name)
        return node is not None and node.running and name not in self.broken_nodes


class SimulatedNetworkBuilder:
    """Drop-in replacement for :class:`NetworkBuilder` with no Mininet."""

    def __init__(self, config: Optional[TopologyConfig] = None, config_path: Optional[str] = None):
        self.config = config or load_topology_config(config_path or DEFAULT_CONFIG_PATH)
        self.net: Optional[SimulatedNet] = None

    def build(self) -> SimulatedNet:
        self.net = SimulatedNet([SimulatedNode(d.name, d.kind) for d in self.config.devices])
        return self.net

    def start(self) -> SimulatedNet:
        if self.net is None:
            self.build()
        return self.net

    def stop(self) -> None:
        self.net = None

    def cleanup(self) -> None:
        self.net = None
