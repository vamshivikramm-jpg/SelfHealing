"""Failure scenarios — reusable orchestrators for failure injection.

Submodule 1.4D. Each Scenario is a thin wrapper around FailureManager
that orchestrates a single failure lifecycle: inject → recover.
Scenarios track their own execution state (IDLE → ACTIVE → RESTORED) and expose
execute(), restore(), status() and summary().
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from network.failure.failure_manager import FailureManager


class ScenarioState(str, Enum):
    IDLE = "IDLE"          # created, not executed yet
    ACTIVE = "ACTIVE"      # failure injected
    RESTORED = "RESTORED"  # failure recovered; the scenario is finished


class Scenario:
    """Base class for all failure scenarios."""

    def __init__(self, manager: FailureManager):
        self.manager = manager
        self._state = ScenarioState.IDLE
        self._failure = None
        self._start_time: Optional[datetime] = None
        self._completed = False

    def execute(self) -> dict:
        if self._state != ScenarioState.IDLE or self._completed:
            raise RuntimeError(
                f"scenario {type(self).__name__} cannot be executed "
                f"in current state: {self._state.value}"
            )
        self._start_time = datetime.now(timezone.utc)
        self._failure = self._inject()
        self._state = ScenarioState.ACTIVE
        return self.summary()

    def restore(self) -> dict:
        if self._state != ScenarioState.ACTIVE:
            raise RuntimeError(
                f"scenario {type(self).__name__} cannot be restored "
                f"from current state: {self._state.value}"
            )
        self._recover()
        self._failure = None
        self._state = ScenarioState.RESTORED
        self._completed = True
        return self.summary()

    def status(self) -> dict:
        return {
            "class": type(self).__name__,
            "state": self._state.value,
            "failure_id": self._failure.id if self._failure else None,
            "start_time": (
                self._start_time.isoformat() if self._start_time else None
            ),
        }

    def summary(self) -> dict:
        return {
            "class": type(self).__name__,
            "state": self._state.value,
            "failure": self._failure.to_dict() if self._failure else None,
            "start_time": (
                self._start_time.isoformat() if self._start_time else None
            ),
        }

    def _inject(self):
        raise NotImplementedError

    def _recover(self) -> None:
        raise NotImplementedError

    def _select_node(self, kind: str) -> str:
        raise ValueError(f"Unknown kind: {kind}")

    def _select_link(self) -> tuple[str, str]:
        raise NotImplementedError


# ======================================================================
# Concrete scenarios
# ======================================================================


class ScenarioRouterFailure(Scenario):
    def _inject(self):
        return self.manager.inject_router_failure("rtr-core")

    def _recover(self) -> None:
        self.manager.recover_router(self._failure.router)


class ScenarioSwitchFailure(Scenario):
    def _inject(self):
        return self.manager.inject_switch_failure("sw-a")

    def _recover(self) -> None:
        self.manager.recover_switch(self._failure.switch)


class ScenarioCableCut(Scenario):
    def _inject(self):
        return self.manager.disconnect_link("sw-a", "h1")

    def _recover(self) -> None:
        self.manager.reconnect_link(self._failure.src, self._failure.dst)


class ScenarioHighLatency(Scenario):
    LATENCY_MS = 100

    def _inject(self):
        return self.manager.inject_latency("sw-a", "h1", self.LATENCY_MS)

    def _recover(self) -> None:
        self.manager.remove_latency("sw-a", "h1")

    def _select_node(self, kind: str) -> str:
        if kind == "router":
            return "rtr-core"
        if kind == "switch":
            return "sw-a"
        raise ValueError(f"Unknown kind: {kind}")


class ScenarioPacketLoss(Scenario):
    LOSS_PERCENT = 10

    def _inject(self):
        return self.manager.inject_packet_loss("sw-a", "h1", self.LOSS_PERCENT)

    def _recover(self) -> None:
        self.manager.remove_packet_loss("sw-a", "h1")


class ScenarioBandwidthConstraint(Scenario):
    BANDWIDTH_MBPS = 10

    def _inject(self):
        return self.manager.limit_bandwidth("sw-a", "h1", self.BANDWIDTH_MBPS)

    def _recover(self) -> None:
        self.manager.remove_bandwidth_limit("sw-a", "h1")

    def _select_node(self, kind: str) -> str:
        if kind == "router":
            return "rtr-core"
        if kind == "switch":
            return "sw-a"
        raise ValueError(f"Unknown kind: {kind}")

    def _select_link(self) -> tuple[str, str]:
        return ("sw-a", "h1")
