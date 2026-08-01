"""NetworkController — the single interface to the virtual enterprise network.

Submodule 1.3. Future modules MUST interact with the Mininet network only
through this class; no module may call Mininet APIs directly.

Every public action:

1. validates the operation against :class:`NetworkState` (safety),
2. applies the operation through the :class:`NetworkBuilder`/Mininet,
3. synchronizes :class:`NetworkState`,
4. publishes an :class:`Event` on the :class:`EventBus`,
5. emits a structured log line.

Safety rules (raise meaningful exceptions):
- start/reset on a running network            -> NetworkAlreadyRunningError
- stop on a stopped network                   -> NetworkNotRunningError
- node/link operations while stopped          -> NetworkNotRunningError
- enable online / disable offline node        -> NodeAlreadyOnline/OfflineError
- connect connected / disconnect disconnected -> LinkAlreadyConnected/DisconnectedError
- unknown node/link                           -> Node/LinkNotFoundError
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from network.config import TopologyConfig
from network.control.exceptions import (
    LinkAlreadyConnectedError,
    LinkAlreadyDisconnectedError,
    LinkNotFoundError,
    NetworkAlreadyRunningError,
    NetworkNotRunningError,
    NodeAlreadyOfflineError,
    NodeAlreadyOnlineError,
    NodeNotFoundError,
)
from network.events.event import Event
from network.events.event_bus import EventBus
from network.events.event_types import EventType
from network.logging import get_logger
from network.network_builder import NetworkBuilder
from network.state.network_state import (
    LINK_CONNECTED,
    LINK_DISCONNECTED,
    NETWORK_STOPPED,
    NODE_OFFLINE,
    NODE_ONLINE,
    NetworkState,
)

SOURCE = "network.controller"


class NetworkController:
    """Owns the NetworkBuilder, NetworkState, EventBus and logging."""

    def __init__(
        self,
        builder: Optional[NetworkBuilder] = None,
        config_path: Optional[str] = None,
        controller_ip: Optional[str] = None,
        controller_port: Optional[int] = None,
        event_bus: Optional[EventBus] = None,
        logger=None,
    ):
        self._builder = builder or NetworkBuilder(
            config_path=config_path,
            controller_ip=controller_ip,
            controller_port=controller_port,
        )
        self._state = NetworkState()
        self._event_bus = event_bus or EventBus()
        self._logger = logger or get_logger("network.control")
        self._net = None
        self._impairments: dict = {}
        self._register_configured_state()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> NetworkState:
        return self._state

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def config(self) -> TopologyConfig:
        return self._builder.config

    # ------------------------------------------------------------------
    # Network lifecycle
    # ------------------------------------------------------------------

    def start_network(self) -> dict:
        if self._state.is_running:
            raise NetworkAlreadyRunningError(
                f"network {self.config.name!r} is already running"
            )
        self._net = self._builder.start()
        self._state.mark_all_nodes(NODE_ONLINE)
        self._state.mark_all_links(LINK_CONNECTED)
        self._state.set_running()
        self._log(EventType.NETWORK_STARTED, "Network Started")
        self._publish(EventType.NETWORK_STARTED, {"name": self.config.name})
        return self._state.to_dict()

    def stop_network(self) -> dict:
        if self._state.is_stopped:
            raise NetworkNotRunningError(
                f"network {self.config.name!r} is not running"
            )
        self._builder.stop()
        self._net = None
        self._state.mark_all_nodes(NODE_OFFLINE)
        self._state.mark_all_links(LINK_DISCONNECTED)
        self._state.set_stopped()
        self._log(EventType.NETWORK_STOPPED, "Network Stopped")
        self._publish(EventType.NETWORK_STOPPED, {"name": self.config.name})
        return self._state.to_dict()

    def restart_network(self) -> dict:
        """Stop (if running) then start. Publishes STOPPED + STARTED."""
        if self._state.is_running:
            self.stop_network()
        return self.start_network()

    def reset_network(self) -> dict:
        """Clean all Mininet artifacts, rebuild and start the network."""
        if self._state.is_running:
            self._builder.stop()
        self._net = None
        self._builder.cleanup()
        self._state.mark_all_nodes(NODE_OFFLINE)
        self._state.mark_all_links(LINK_DISCONNECTED)
        self._state.set_stopped()
        self.start_network()
        self._log(EventType.NETWORK_RESET, "Network Reset")
        self._publish(EventType.NETWORK_RESET, {"name": self.config.name})
        return self._state.to_dict()

    # ------------------------------------------------------------------
    # Node control
    # ------------------------------------------------------------------

    def enable_node(self, node_name: str) -> dict:
        self._require_running()
        node_state = self._require_node(node_name)
        if node_state.is_online:
            raise NodeAlreadyOnlineError(f"node {node_name!r} is already online")
        self._net.get(node_name).start()
        node_state.set_online()
        self._log(EventType.NODE_ENABLED, f"Node {node_name} Enabled")
        self._publish(EventType.NODE_ENABLED, {"node": node_name})
        return node_state.to_dict()

    def disable_node(self, node_name: str) -> dict:
        self._require_running()
        node_state = self._require_node(node_name)
        if node_state.is_offline:
            raise NodeAlreadyOfflineError(f"node {node_name!r} is already offline")
        self._net.get(node_name).stop()
        node_state.set_offline()
        self._log(EventType.NODE_DISABLED, f"Node {node_name} Disabled")
        self._publish(EventType.NODE_DISABLED, {"node": node_name})
        return node_state.to_dict()

    # ------------------------------------------------------------------
    # Link control
    # ------------------------------------------------------------------

    def connect_link(self, node1: str, node2: str) -> dict:
        self._require_running()
        link_state = self._require_link(node1, node2)
        if link_state.is_connected:
            raise LinkAlreadyConnectedError(
                f"link {link_state.name!r} is already connected"
            )
        self._net.configLinkStatus(node1, node2, "up")
        link_state.set_connected()
        self._log(EventType.LINK_CONNECTED, f"Link {link_state.name} Connected")
        self._publish(
            EventType.LINK_CONNECTED, {"src": node1, "dst": node2}
        )
        return link_state.to_dict()

    def disconnect_link(self, node1: str, node2: str) -> dict:
        self._require_running()
        link_state = self._require_link(node1, node2)
        if link_state.is_disconnected:
            raise LinkAlreadyDisconnectedError(
                f"link {link_state.name!r} is already disconnected"
            )
        self._net.configLinkStatus(node1, node2, "down")
        link_state.set_disconnected()
        self._log(EventType.LINK_DISCONNECTED, f"Link {link_state.name} Disconnected")
        self._publish(
            EventType.LINK_DISCONNECTED, {"src": node1, "dst": node2}
        )
        return link_state.to_dict()

    # ------------------------------------------------------------------
    # Network impairments (Submodule 1.4C.1)
    # ------------------------------------------------------------------
    #
    # Traffic-control APIs exposed by the controller (the only layer that
    # talks to Mininet). Each set/clear validates the target link and the
    # parameter range, updates the controller's impairment state, and
    # applies the change to the data plane. Applying is best-effort: the
    # controller calls ``configLinkImpairment(key, params)`` on the
    # network if it is available (mocked in unit tests / a TCLink-backed
    # Mininet); on a stock Mininet link the state is still tracked.

    def set_link_latency(self, node1: str, node2: str, latency_ms) -> dict:
        """Set added one-way latency (ms) on a link. Raises ``ValueError``
        for negative latency, ``LinkNotFoundError`` for unknown links."""
        self._require_running_and_link(node1, node2)
        if latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0 (got {latency_ms})")
        return self._set_impairment(node1, node2, "latency_ms", float(latency_ms))

    def clear_link_latency(self, node1: str, node2: str) -> dict:
        self._require_running_and_link(node1, node2)
        return self._set_impairment(node1, node2, "latency_ms", None)

    def set_link_packet_loss(self, node1: str, node2: str, loss_percent) -> dict:
        """Set packet-loss percentage on a link. Raises ``ValueError`` if
        loss is outside [0, 100]."""
        self._require_running_and_link(node1, node2)
        if not (0 <= loss_percent <= 100):
            raise ValueError(f"loss_percent must be within [0, 100] (got {loss_percent})")
        return self._set_impairment(node1, node2, "loss_percent", float(loss_percent))

    def clear_link_packet_loss(self, node1: str, node2: str) -> dict:
        self._require_running_and_link(node1, node2)
        return self._set_impairment(node1, node2, "loss_percent", None)

    def set_link_bandwidth(self, node1: str, node2: str, bandwidth_mbps) -> dict:
        """Set link bandwidth (Mbps). Raises ``ValueError`` if bandwidth
        is <= 0."""
        self._require_running_and_link(node1, node2)
        if bandwidth_mbps <= 0:
            raise ValueError(f"bandwidth_mbps must be > 0 (got {bandwidth_mbps})")
        return self._set_impairment(node1, node2, "bandwidth_mbps", float(bandwidth_mbps))

    def clear_link_bandwidth(self, node1: str, node2: str) -> dict:
        self._require_running_and_link(node1, node2)
        return self._set_impairment(node1, node2, "bandwidth_mbps", None)

    def impairments(self) -> dict:
        """Active link impairments keyed by canonical link name."""
        return {key: dict(settings) for key, settings in self._impairments.items()}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def network_status(self) -> dict:
        """Current network status from the synchronized state."""
        return {
            "status": self._state.status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": [
                n.to_dict() for n in sorted(self._state.nodes.values(), key=lambda n: n.name)
            ],
            "links": [
                l.to_dict() for l in sorted(self._state.links.values(), key=lambda l: l.name)
            ],
        }

    def topology_snapshot(self) -> dict:
        """Serializable snapshot: topology structure + live state."""
        config = self.config
        link_index = {
            tuple(sorted((link.src, link.dst))): link
            for link in self._state.links.values()
        }
        return {
            "name": config.name,
            "version": config.version,
            "status": self._state.status,
            "segments": [
                {
                    "id": segment.id,
                    "label": segment.label,
                    "subnets": [
                        {"id": s.id, "cidr": s.cidr, "gateway": s.gateway}
                        for s in segment.subnets
                    ],
                }
                for segment in config.segments
            ],
            "devices": [
                {
                    "name": dev.name,
                    "kind": dev.kind,
                    "role": dev.role,
                    "subnet": dev.subnet,
                    "ip": dev.ip,
                    "gateway": dev.gateway,
                    "services": list(dev.services),
                    "interfaces": [
                        {"port": i.port, "name": i.name, "subnet": i.subnet, "ip": i.ip}
                        for i in dev.interfaces
                    ],
                    "datapath_id": dev.datapath_id,
                    "state": self._state.get_node(dev.name).state,
                }
                for dev in config.devices
            ],
            "links": [
                {
                    "src": link.src,
                    "dst": link.dst,
                    "src_port": link.src_port,
                    "dst_port": link.dst_port,
                    "state": link_index[tuple(sorted((link.src, link.dst)))].state,
                }
                for link in config.links
            ],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _register_configured_state(self) -> None:
        for dev in self.config.devices:
            self._state.register_device(dev.name, dev.kind)
        for link in self.config.links:
            self._state.register_link(link.src, link.dst)

    def _require_running(self) -> None:
        if not self._state.is_running or self._net is None:
            raise NetworkNotRunningError("network is not running")

    def _require_node(self, node_name: str):
        node_state = self._state.get_node(node_name)
        if node_state is None:
            raise NodeNotFoundError(f"node {node_name!r} does not exist in the topology")
        return node_state

    def _require_link(self, node1: str, node2: str):
        link_state = self._state.get_link(node1, node2)
        if link_state is None:
            raise LinkNotFoundError(
                f"link between {node1!r} and {node2!r} does not exist in the topology"
            )
        return link_state

    def _require_running_and_link(self, node1: str, node2: str):
        self._require_running()
        return self._require_link(node1, node2)

    @staticmethod
    def _link_key(node1: str, node2: str) -> str:
        return "-".join(sorted((node1, node2)))

    def _set_impairment(self, node1: str, node2: str, kind: str, value) -> dict:
        key = self._link_key(node1, node2)
        entry = self._impairments.setdefault(key, {})
        if value is None:
            entry.pop(kind, None)
            if not entry:
                self._impairments.pop(key, None)
        else:
            entry[kind] = value
        self._apply_impairment(key, dict(entry))
        return dict(entry)

    def _apply_impairment(self, key: str, params: dict) -> None:
        apply = getattr(self._net, "configLinkImpairment", None)
        if apply is not None:
            apply(key, params)

    def _log(self, event_type: EventType, message: str) -> None:
        self._logger.info(
            message,
            extra={"event": event_type.value, "result": "success"},
        )

    def _publish(self, event_type: EventType, data: dict) -> None:
        self._event_bus.publish(
            Event(type=event_type, source=SOURCE, data=data)
        )
