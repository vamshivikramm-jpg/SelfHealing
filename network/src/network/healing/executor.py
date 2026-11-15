"""Executing intents (Submodule 7.3) and undoing them (7.5).

``SimulatedExecutor`` turns Module 6 intents into concrete actions on the
simulated network:

| Intent | Action | Undo |
|---|---|---|
| RESTORE link / node | recover the failure through the FailureManager, or reconnect / re-enable through the NetworkController | none possible: a failed restore is escalated |
| REROUTE / LOAD_BALANCE | install the new routes in the data plane (ServiceModel) | reinstall the previous routes |
| QUARANTINE / BLOCK / ISOLATE | add a manual block in the Zero Trust engine (its switch rules drop the device's traffic) | remove the block |
| MONITOR | nothing (alert only) | — |

A command that returns without error has only *executed*; whether it
worked is decided by the verifier from telemetry and traffic delivery.

A live executor (NetworkController on Mininet + Ryu REST) can implement
the same two methods once the Linux lab exists; re-enabling nodes on real
Mininet first needs the Module 2.0 fixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from network.decision.intents import IntentType


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    undo: Dict[str, Any] = field(default_factory=dict)


class SimulatedExecutor:
    def __init__(self, controller, manager, service, security=None):
        self.controller = controller
        self.manager = manager
        self.service = service
        self.security = security

    # -- execute ------------------------------------------------------------------

    def execute(self, intent: dict) -> ExecutionResult:
        kind = IntentType(intent["type"])
        try:
            if kind == IntentType.RESTORE:
                return self._restore(intent)
            if kind in (IntentType.REROUTE, IntentType.LOAD_BALANCE):
                return self._install_routes(intent)
            if kind in (IntentType.QUARANTINE, IntentType.BLOCK, IntentType.ISOLATE):
                return self._block(intent)
            return ExecutionResult(True, "monitor only: nothing to execute")
        except Exception as exc:  # noqa: BLE001 - reported as a failed execution
            return ExecutionResult(False, f"{type(exc).__name__}: {exc}")

    def _restore(self, intent: dict) -> ExecutionResult:
        kind, name = intent["target"].split(":", 1)
        if kind == "node":
            failure = self._active_failure(lambda f: getattr(f, "router", None) == name
                                           or getattr(f, "switch", None) == name)
            if failure is not None:
                if getattr(failure, "router", None) == name:
                    self.manager.recover_router(name)
                else:
                    self.manager.recover_switch(name)
                return ExecutionResult(True, f"recovered failure on {name}")
            if self.controller.state.get_node(name).is_offline:
                self.controller.enable_node(name)
                return ExecutionResult(True, f"re-enabled {name}")
            return ExecutionResult(True, f"{name} already enabled in the controller")
        a, b = self.service.graph.endpoints(name)
        failure = self._active_failure(lambda f: hasattr(f, "src") and {f.src, f.dst} == {a, b}
                                       and type(f).__name__ == "LinkFailure")
        if failure is not None:
            self.manager.reconnect_link(failure.src, failure.dst)
            return ExecutionResult(True, f"reconnected link {name}")
        if self.controller.state.get_link(a, b).is_disconnected:
            self.controller.connect_link(a, b)
            return ExecutionResult(True, f"connected link {name}")
        return ExecutionResult(True, f"link {name} already connected in the controller")

    def _active_failure(self, match):
        return next((f for f in self.manager.active_failures() if match(f)), None)

    def _install_routes(self, intent: dict) -> ExecutionResult:
        previous = {}
        for move in intent["params"].get("moves", []):
            pair = (move["src"], move["dst"])
            previous[pair] = self.service.routes[pair]
            self.service.routes[pair] = tuple(move["new"])
        return ExecutionResult(True, f"installed {len(previous)} route(s)", {"routes": previous})

    def _block(self, intent: dict) -> ExecutionResult:
        if self.security is None:
            return ExecutionResult(False, "no enforcement backend for security actions")
        subject = intent["params"].get("subject") or intent["target"].split(":", 1)[1]
        added = self.security.add_block(subject)
        return ExecutionResult(True, f"blocking {subject}", {"block": subject if added else None})

    # -- undo ---------------------------------------------------------------------

    def undo(self, intent: dict, undo: Dict[str, Any]) -> str:
        if "routes" in undo:
            for pair, path in undo["routes"].items():
                self.service.routes[pair] = path
            return f"restored {len(undo['routes'])} previous route(s)"
        if undo.get("block") and self.security is not None:
            self.security.remove_block(undo["block"])
            return f"unblocked {undo['block']}"
        return "nothing to undo"
