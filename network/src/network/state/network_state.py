"""Network state model (Submodule 1.3).

NetworkController keeps a live :class:`NetworkState` mirroring the real
Mininet network:

- Network: running / stopped
- Nodes: online / offline
- Links: connected / disconnected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

NETWORK_RUNNING = "running"
NETWORK_STOPPED = "stopped"

NODE_ONLINE = "online"
NODE_OFFLINE = "offline"

LINK_CONNECTED = "connected"
LINK_DISCONNECTED = "disconnected"


def _canonical_pair(a: str, b: str) -> tuple:
    return (a, b) if a <= b else (b, a)


def _link_name(a: str, b: str) -> str:
    x, y = _canonical_pair(a, b)
    return f"{x}-{y}"


@dataclass
class NodeState:
    """Per-device state (Router/Switch/Host/Server)."""

    name: str
    kind: str
    state: str = NODE_OFFLINE

    @property
    def is_online(self) -> bool:
        return self.state == NODE_ONLINE

    @property
    def is_offline(self) -> bool:
        return self.state == NODE_OFFLINE

    def set_online(self) -> None:
        self.state = NODE_ONLINE

    def set_offline(self) -> None:
        self.state = NODE_OFFLINE

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "state": self.state}


@dataclass
class LinkState:
    """Per-link state (device pair). Names are canonical (sorted)."""

    src: str
    dst: str
    state: str = LINK_DISCONNECTED

    def __post_init__(self) -> None:
        if self.src > self.dst:
            self.src, self.dst = self.dst, self.src

    @property
    def name(self) -> str:
        return _link_name(self.src, self.dst)

    @property
    def is_connected(self) -> bool:
        return self.state == LINK_CONNECTED

    @property
    def is_disconnected(self) -> bool:
        return self.state == LINK_DISCONNECTED

    def set_connected(self) -> None:
        self.state = LINK_CONNECTED

    def set_disconnected(self) -> None:
        self.state = LINK_DISCONNECTED

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "state": self.state}


class NetworkState:
    """Single source of truth for the state of the virtual network."""

    def __init__(self):
        self.status: str = NETWORK_STOPPED
        self.nodes: Dict[str, NodeState] = {}
        self.links: Dict[tuple, LinkState] = {}

    # -- registration -----------------------------------------------------

    def register_device(self, name: str, kind: str) -> NodeState:
        node = self.nodes.get(name)
        if node is None:
            node = NodeState(name=name, kind=kind)
            self.nodes[name] = node
        return node

    def register_link(self, src: str, dst: str) -> LinkState:
        key = _canonical_pair(src, dst)
        link = self.links.get(key)
        if link is None:
            link = LinkState(src=key[0], dst=key[1])
            self.links[key] = link
        return link

    # -- network-level transitions -----------------------------------------

    @property
    def is_running(self) -> bool:
        return self.status == NETWORK_RUNNING

    @property
    def is_stopped(self) -> bool:
        return self.status == NETWORK_STOPPED

    def set_running(self) -> None:
        self.status = NETWORK_RUNNING

    def set_stopped(self) -> None:
        self.status = NETWORK_STOPPED

    # -- node transitions ---------------------------------------------------

    def get_node(self, name: str) -> Optional[NodeState]:
        return self.nodes.get(name)

    def set_node_online(self, name: str) -> NodeState:
        node = self._require_node(name)
        node.set_online()
        return node

    def set_node_offline(self, name: str) -> NodeState:
        node = self._require_node(name)
        node.set_offline()
        return node

    # -- link transitions ---------------------------------------------------

    def get_link(self, src: str, dst: str) -> Optional[LinkState]:
        return self.links.get(_canonical_pair(src, dst))

    def set_link_connected(self, src: str, dst: str) -> LinkState:
        link = self._require_link(src, dst)
        link.set_connected()
        return link

    def set_link_disconnected(self, src: str, dst: str) -> LinkState:
        link = self._require_link(src, dst)
        link.set_disconnected()
        return link

    # -- bulk transitions (used by start/stop) --------------------------------

    def mark_all_nodes(self, state: str) -> None:
        for node in self.nodes.values():
            node.state = state

    def mark_all_links(self, state: str) -> None:
        for link in self.links.values():
            link.state = state

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda n: n.name)],
            "links": [l.to_dict() for l in sorted(self.links.values(), key=lambda l: l.name)],
        }

    def _require_node(self, name: str) -> NodeState:
        node = self.nodes.get(name)
        if node is None:
            raise KeyError(f"node not registered in network state: {name!r}")
        return node

    def _require_link(self, src: str, dst: str) -> LinkState:
        link = self.get_link(src, dst)
        if link is None:
            raise KeyError(f"link not registered in network state: {src!r}-{dst!r}")
        return link
