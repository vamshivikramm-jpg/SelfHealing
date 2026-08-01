"""FailureManager — the ONLY interface for injecting and recovering failures.

Submodule 1.4B.1 implements router and switch failure injection/recovery.
Submodule 1.4B.2 adds link failure support (disconnect_link /
reconnect_link) and reset_all_failures(). Submodule 1.4C.2 adds
network impairment support (latency, packet loss, bandwidth) via the
NetworkController.

The FailureManager never touches Mininet; every data-plane operation is
performed through the :class:`NetworkController`. State is tracked in the
:class:`FailureRegistry`; every operation publishes an :class:`Event` on
the controller's :class:`EventBus` and emits a structured JSON log line.
"""

from __future__ import annotations

from network.control import NetworkController
from network.events import Event, EventType
from network.failure.exceptions import (
    DuplicateFailureError,
    FailureError,
    FailureNotFoundError,
    InvalidBandwidthError,
    InvalidLatencyError,
    InvalidPacketLossError,
    UnknownLinkError,
    UnknownNodeError,
)
from network.failure.failure_registry import FailureRegistry
from network.failure.failure_types import (
    BandwidthFailure,
    Failure,
    FailureState,
    LatencyFailure,
    LinkFailure,
    PacketLossFailure,
    RouterFailure,
    SwitchFailure,
)
from network.logging import get_logger

SOURCE = "failure.manager"
INJECTOR = "FailureManager"


class FailureManager:
    """Inject and recover router/switch failures through the controller."""

    def __init__(self, controller, registry=None, logger=None):
        self._controller = controller
        self._registry = registry or FailureRegistry(
            valid_nodes={d.name for d in controller.config.devices}
        )
        self._logger = logger or get_logger("failure.manager")

    # ------------------------------------------------------------------
    # Router failures
    # ------------------------------------------------------------------

    def inject_router_failure(self, node_name: str) -> Failure:
        """Take a router down and record the failure.

        Raises :class:`UnknownNodeError` for unknown nodes,
        :class:`FailureError` if the node is not a router, and
        :class:`DuplicateFailureError` if a router failure is already
        active on the node.
        """
        self._require_device(node_name, "router")
        if self._active_router_failure(node_name) is not None:
            raise DuplicateFailureError(
                f"a router failure is already active on node {node_name!r}"
            )
        failure = RouterFailure(router=node_name, injected_by=INJECTOR)
        self._controller.disable_node(node_name)
        self._registry.register(failure)
        self._publish(
            EventType.FAILURE_INJECTED,
            {"failure_id": failure.id, "node": node_name},
        )
        self._log(EventType.FAILURE_INJECTED, f"Router failure injected on {node_name}")
        return failure

    def recover_router(self, node_name: str) -> Failure:
        """Bring a failed router back up and mark the failure recovered.

        Raises :class:`UnknownNodeError` for unknown nodes,
        :class:`FailureError` if the node is not a router, and
        :class:`FailureNotFoundError` if no router failure is active on
        the node (recovering a healthy device).
        """
        self._require_device(node_name, "router")
        failure = self._active_router_failure(node_name)
        if failure is None:
            raise FailureNotFoundError(
                f"no active router failure on node {node_name!r}"
            )
        self._controller.enable_node(node_name)
        self._registry.mark_recovered(failure.id)
        self._publish(
            EventType.FAILURE_RECOVERED,
            {"failure_id": failure.id, "node": node_name},
        )
        self._log(EventType.FAILURE_RECOVERED, f"Router failure recovered on {node_name}")
        return failure

    # ------------------------------------------------------------------
    # Switch failures
    # ------------------------------------------------------------------

    def inject_switch_failure(self, node_name: str) -> Failure:
        """Take a switch down and record the failure.

        Raises :class:`UnknownNodeError` for unknown nodes,
        :class:`FailureError` if the node is not a switch, and
        :class:`DuplicateFailureError` if a switch failure is already
        active on the node.
        """
        self._require_device(node_name, "switch")
        if self._active_switch_failure(node_name) is not None:
            raise DuplicateFailureError(
                f"a switch failure is already active on node {node_name!r}"
            )
        failure = SwitchFailure(switch=node_name, injected_by=INJECTOR)
        self._controller.disable_node(node_name)
        self._registry.register(failure)
        self._publish(
            EventType.FAILURE_INJECTED,
            {"failure_id": failure.id, "node": node_name},
        )
        self._log(EventType.FAILURE_INJECTED, f"Switch failure injected on {node_name}")
        return failure

    def recover_switch(self, node_name: str) -> Failure:
        """Bring a failed switch back up and mark the failure recovered.

        Raises :class:`UnknownNodeError` for unknown nodes,
        :class:`FailureError` if the node is not a switch, and
        :class:`FailureNotFoundError` if no switch failure is active on
        the node (recovering a healthy device).
        """
        self._require_device(node_name, "switch")
        failure = self._active_switch_failure(node_name)
        if failure is None:
            raise FailureNotFoundError(
                f"no active switch failure on node {node_name!r}"
            )
        self._controller.enable_node(node_name)
        self._registry.mark_recovered(failure.id)
        self._publish(
            EventType.FAILURE_RECOVERED,
            {"failure_id": failure.id, "node": node_name},
        )
        self._log(EventType.FAILURE_RECOVERED, f"Switch failure recovered on {node_name}")
        return failure

    # ------------------------------------------------------------------
    # Link failures
    # ------------------------------------------------------------------

    def disconnect_link(self, node1: str, node2: str) -> Failure:
        """Take a link down and record the failure.

        Raises :class:`UnknownLinkError` for unknown links, and
        :class:`DuplicateFailureError` if a link failure is already
        active on the pair (an already-disconnected link).
        """
        self._require_link(node1, node2)
        if self._active_link_failure(node1, node2) is not None:
            raise DuplicateFailureError(
                f"a link failure is already active between {node1!r} and {node2!r}"
            )
        failure = LinkFailure(src=node1, dst=node2, injected_by=INJECTOR)
        self._controller.disconnect_link(node1, node2)
        self._registry.register(failure)
        self._log(EventType.LINK_DISCONNECTED, f"Link {failure.link} disconnected")
        return failure

    def reconnect_link(self, node1: str, node2: str) -> Failure:
        """Bring a disconnected link back up and mark the failure recovered.

        Raises :class:`UnknownLinkError` for unknown links, and
        :class:`FailureNotFoundError` if no link failure is active on the
        pair (an already-connected link).
        """
        self._require_link(node1, node2)
        failure = self._active_link_failure(node1, node2)
        if failure is None:
            raise FailureNotFoundError(
                f"no active link failure between {node1!r} and {node2!r}"
            )
        self._controller.connect_link(node1, node2)
        self._registry.mark_recovered(failure.id)
        self._log(EventType.LINK_CONNECTED, f"Link {failure.link} reconnected")
        return failure

    # ------------------------------------------------------------------
    # Network impairments (Submodule 1.4C.2)
    # ------------------------------------------------------------------
    #
    # All impairment operations delegate to the NetworkController.
    # No impairment logic lives here; the controller is the sole
    # layer that talks to Mininet.

    def inject_latency(self, node1: str, node2: str, latency_ms) -> Failure:
        """Impose added one-way latency on a link.

        Calls :meth:`NetworkController.set_link_latency`. Raises
        :class:`InvalidLatencyError` for negative latency,
        :class:`UnknownLinkError` for unknown links, and
        :class:`DuplicateFailureError` if a latency impairment is
        already active on the pair.
        """
        self._require_link(node1, node2)
        if latency_ms < 0:
            raise InvalidLatencyError(
                f"latency_ms must be >= 0 (got {latency_ms})"
            )
        if self._active_impairment_for(
            LatencyFailure, node1, node2, "latency_ms"
        ) is not None:
            raise DuplicateFailureError(
                f"a latency impairment is already active on "
                f"{node1!r}-{node2!r}"
            )
        failure = LatencyFailure(
            node=node1, latency_ms=latency_ms, injected_by=INJECTOR
        )
        self._controller.set_link_latency(node1, node2, latency_ms)
        self._registry.register(failure)
        self._publish(
            EventType.LATENCY_INJECTED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.LATENCY_INJECTED,
            f"Latency {latency_ms} ms injected on {node1}-{node2}",
        )
        return failure

    def remove_latency(self, node1: str, node2: str) -> Failure:
        """Remove a latency impairment from a link.

        Calls :meth:`NetworkController.clear_link_latency`. Raises
        :class:`UnknownLinkError` for unknown links and
        :class:`FailureNotFoundError` if no latency impairment is
        active on the pair.
        """
        self._require_link(node1, node2)
        failure = self._active_impairment_for(
            LatencyFailure, node1, node2, "latency_ms"
        )
        if failure is None:
            raise FailureNotFoundError(
                f"no active latency impairment on {node1!r}-{node2!r}"
            )
        self._controller.clear_link_latency(node1, node2)
        self._registry.mark_recovered(failure.id)
        self._publish(
            EventType.FAILURE_RECOVERED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.FAILURE_RECOVERED,
            f"Latency impairment removed on {node1}-{node2}",
        )
        return failure

    def inject_packet_loss(
        self, node1: str, node2: str, loss_percent
    ) -> Failure:
        """Impose packet loss on a link.

        Calls :meth:`NetworkController.set_link_packet_loss`. Raises
        :class:`InvalidPacketLossError` for loss outside [0, 100],
        :class:`UnknownLinkError` for unknown links, and
        :class:`DuplicateFailureError` if a packet-loss impairment
        is already active on the pair.
        """
        self._require_link(node1, node2)
        if not (0 <= loss_percent <= 100):
            raise InvalidPacketLossError(
                f"loss_percent must be within [0, 100] (got {loss_percent})"
            )
        if self._active_impairment_for(
            PacketLossFailure, node1, node2, "loss_percent"
        ) is not None:
            raise DuplicateFailureError(
                f"a packet-loss impairment is already active on "
                f"{node1!r}-{node2!r}"
            )
        failure = PacketLossFailure(
            node=node1, loss_percent=loss_percent, injected_by=INJECTOR
        )
        self._controller.set_link_packet_loss(node1, node2, loss_percent)
        self._registry.register(failure)
        self._publish(
            EventType.PACKET_LOSS_INJECTED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.PACKET_LOSS_INJECTED,
            f"Packet loss {loss_percent}% injected on {node1}-{node2}",
        )
        return failure

    def remove_packet_loss(self, node1: str, node2: str) -> Failure:
        """Remove a packet-loss impairment from a link.

        Calls :meth:`NetworkController.clear_link_packet_loss`. Raises
        :class:`UnknownLinkError` for unknown links and
        :class:`FailureNotFoundError` if no packet-loss impairment is
        active on the pair.
        """
        self._require_link(node1, node2)
        failure = self._active_impairment_for(
            PacketLossFailure, node1, node2, "loss_percent"
        )
        if failure is None:
            raise FailureNotFoundError(
                f"no active packet-loss impairment on {node1!r}-{node2!r}"
            )
        self._controller.clear_link_packet_loss(node1, node2)
        self._registry.mark_recovered(failure.id)
        self._publish(
            EventType.FAILURE_RECOVERED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.FAILURE_RECOVERED,
            f"Packet-loss impairment removed on {node1}-{node2}",
        )
        return failure

    def limit_bandwidth(
        self, node1: str, node2: str, bandwidth_mbps
    ) -> Failure:
        """Impose a bandwidth cap on a link.

        Calls :meth:`NetworkController.set_link_bandwidth`. Raises
        :class:`InvalidBandwidthError` for bandwidth <= 0,
        :class:`UnknownLinkError` for unknown links, and
        :class:`DuplicateFailureError` if a bandwidth impairment is
        already active on the pair.
        """
        self._require_link(node1, node2)
        if bandwidth_mbps <= 0:
            raise InvalidBandwidthError(
                f"bandwidth_mbps must be > 0 (got {bandwidth_mbps})"
            )
        if self._active_impairment_for(
            BandwidthFailure, node1, node2, "bandwidth_bps"
        ) is not None:
            raise DuplicateFailureError(
                f"a bandwidth impairment is already active on "
                f"{node1!r}-{node2!r}"
            )
        failure = BandwidthFailure(
            node=node1, bandwidth_bps=bandwidth_mbps, injected_by=INJECTOR
        )
        self._controller.set_link_bandwidth(node1, node2, bandwidth_mbps)
        self._registry.register(failure)
        self._publish(
            EventType.BANDWIDTH_LIMITED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.BANDWIDTH_LIMITED,
            f"Bandwidth limited to {bandwidth_mbps} Mbps on "
            f"{node1}-{node2}",
        )
        return failure

    def remove_bandwidth_limit(self, node1: str, node2: str) -> Failure:
        """Remove a bandwidth impairment from a link.

        Calls :meth:`NetworkController.clear_link_bandwidth`. Raises
        :class:`UnknownLinkError` for unknown links and
        :class:`FailureNotFoundError` if no bandwidth impairment is
        active on the pair.
        """
        self._require_link(node1, node2)
        failure = self._active_impairment_for(
            BandwidthFailure, node1, node2, "bandwidth_bps"
        )
        if failure is None:
            raise FailureNotFoundError(
                f"no active bandwidth impairment on {node1!r}-{node2!r}"
            )
        self._controller.clear_link_bandwidth(node1, node2)
        self._registry.mark_recovered(failure.id)
        self._publish(
            EventType.FAILURE_RECOVERED,
            {"failure_id": failure.id, "src": node1, "dst": node2},
        )
        self._log(
            EventType.FAILURE_RECOVERED,
            f"Bandwidth impairment removed on {node1}-{node2}",
        )
        return failure

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def active_failures(self) -> list:
        """Failures still in :class:`FailureState.ACTIVE`."""
        return self._registry.active_failures()

    def reset_all_failures(self) -> list:
        """Recover every active failure and publish NETWORK_RESTORED.

        Each individual failure is recovered through the corresponding
        public method (recover_router / recover_switch / reconnect_link),
        so their usual events are emitted. A single NETWORK_RESTORED
        event is then published. Returns the full failure history.
        """
        active = list(self._registry.active_failures())
        for failure in active:
            if isinstance(failure, RouterFailure):
                self.recover_router(failure.router)
            elif isinstance(failure, SwitchFailure):
                self.recover_switch(failure.switch)
            elif isinstance(failure, LinkFailure):
                self.reconnect_link(failure.src, failure.dst)
        self._publish(EventType.NETWORK_RESTORED, {"recovered": len(active)})
        self._log(EventType.NETWORK_RESTORED, f"All {len(active)} failures recovered")
        return self._registry.history()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_device(self, node_name: str, kind: str):
        dev = self._controller.config.device(node_name)
        if dev is None:
            raise UnknownNodeError(f"unknown node {node_name!r}")
        if dev.kind != kind:
            raise FailureError(f"node {node_name!r} is not a {kind}")
        return dev

    def _active_router_failure(self, node_name: str):
        return next(
            (
                f
                for f in self._registry.active_failures()
                if isinstance(f, RouterFailure) and f.router == node_name
            ),
            None,
        )

    def _active_switch_failure(self, node_name: str):
        return next(
            (
                f
                for f in self._registry.active_failures()
                if isinstance(f, SwitchFailure) and f.switch == node_name
            ),
            None,
        )

    def _require_link(self, node1: str, node2: str) -> None:
        if node1 == node2:
            raise UnknownLinkError(
                f"invalid link {node1!r}-{node2!r}: endpoints must differ"
            )
        exists = any(
            (l.src == node1 and l.dst == node2)
            or (l.src == node2 and l.dst == node1)
            for l in self._controller.config.links
        )
        if not exists:
            raise UnknownLinkError(f"unknown link {node1!r}-{node2!r}")

    def _active_link_failure(self, node1: str, node2: str):
        target = "-".join(sorted((node1, node2)))
        return next(
            (
                f
                for f in self._registry.active_failures()
                if isinstance(f, LinkFailure) and f.link == target
            ),
            None,
        )

    def _active_impairment_for(
        self, cls, node1: str, node2: str, kind: str
    ):
        return next(
            (
                f
                for f in self._registry.active_failures()
                if isinstance(f, cls)
                and f.node in (node1, node2)
                and getattr(f, kind, None) is not None
            ),
            None,
        )

    def _publish(self, event_type: EventType, data: dict) -> None:
        self._controller.event_bus.publish(
            Event(type=event_type, source=SOURCE, data=data)
        )

    def _log(self, event_type: EventType, message: str) -> None:
        self._logger.info(
            message,
            extra={"event": event_type.value, "result": "success"},
        )
