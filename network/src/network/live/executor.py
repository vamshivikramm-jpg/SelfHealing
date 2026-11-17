"""Healing actions on the live network (Modules 7.3-7.5, live).

:class:`LiveExecutor` keeps the simulated executor's RESTORE logic (the
FailureManager and NetworkController drive real Mininet since live P1) and
replaces the two actions that only touched in-memory models:

| Intent | Live action | Undo |
|---|---|---|
| REROUTE / LOAD_BALANCE | pin each moved demand's switch path in Ryu, for both directions (``PUT /sh/routes/<ip>``) | restore the previous pins |
| QUARANTINE / BLOCK / ISOLATE | drop rules for the device's MAC and IP in every switch (``POST /sh/blocks``), plus the Zero Trust engine's own block | delete those rules |

Ryu pins paths per *destination*, so two moves towards the same server
with different paths share the last one; every pin is validated by the
controller (live links, ends at the right switch) and rejected otherwise.

:class:`LiveVerifier` checks ``access_blocked`` with a real ping from the
device to a server (blocked = nothing gets through); the other checks read
live telemetry and the live service model unchanged.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from network.healing.executor import ExecutionResult, SimulatedExecutor
from network.healing.verify import Verifier

FORWARDING = ("router", "switch")


class LiveExecutor(SimulatedExecutor):
    def __init__(self, controller, manager, service, client, security=None):
        super().__init__(controller, manager, service, security)
        self.client = client
        self.kinds = {d.name: d.kind for d in controller.config.devices}
        self.ip = {d.name: d.ip.split("/")[0] for d in controller.config.host_devices}
        self.macs = {}
        if security is not None:
            self.macs = {i.name: i.mac for i in security.registry.all()}

    def _install_routes(self, intent: dict) -> ExecutionResult:
        moves = intent["params"].get("moves", [])
        try:
            current = self.client.routes()
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(False, f"cannot read routes: {exc}")
        previous_routes, previous_pins = {}, {}
        try:
            for move in moves:
                switches = [n for n in move["new"] if self.kinds.get(n) in FORWARDING]
                for dst, path in ((move["dst"], switches), (move["src"], list(reversed(switches)))):
                    ip = self.ip[dst]
                    previous_pins.setdefault(ip, (current.get(ip) or {}).get("pinned"))
                    self.client.pin(ip, path)
                pair = (move["src"], move["dst"])
                previous_routes[pair] = self.service.routes.get(pair)
                self.service.routes[pair] = tuple(move["new"])
        except Exception as exc:  # noqa: BLE001 - undo what was already pinned
            self._restore_pins(previous_pins)
            for pair, path in previous_routes.items():
                self.service.routes[pair] = path
            return ExecutionResult(False, f"pinning failed: {exc}")
        return ExecutionResult(True, f"pinned {len(previous_pins)} path(s) in the controller",
                               {"routes": previous_routes, "pins": previous_pins})

    def _restore_pins(self, pins: Dict[str, Optional[list]]) -> None:
        for ip, path in pins.items():
            try:
                if path:
                    self.client.pin(ip, path)
                else:
                    self.client.unpin(ip)
            except Exception:  # noqa: BLE001 - best effort; the path may be gone
                pass

    def _block(self, intent: dict) -> ExecutionResult:
        subject = intent["params"].get("subject") or intent["target"].split(":", 1)[1]
        matches = []
        if self.macs.get(subject):
            matches.append({"eth_src": self.macs[subject]})
        if subject in self.ip:
            matches.append({"ipv4_src": self.ip[subject]})
        if not matches:
            return ExecutionResult(False, f"unknown device {subject!r}")
        ids = []
        try:
            for match in matches:
                ids.append(self.client.block(match, f"{intent['type']} {subject}")["id"])
        except Exception as exc:  # noqa: BLE001
            for block_id in ids:
                self.client.unblock(block_id)
            return ExecutionResult(False, f"blocking failed: {exc}")
        added = self.security.add_block(subject) if self.security is not None else False
        return ExecutionResult(True, f"blocking {subject} in every switch",
                               {"block": subject if added else None, "ryu_blocks": ids})

    def undo(self, intent: dict, undo: dict) -> str:
        notes = []
        if "pins" in undo:
            self._restore_pins(undo["pins"])
            notes.append(f"restored {len(undo['pins'])} pin(s)")
        for block_id in undo.get("ryu_blocks", []):
            try:
                self.client.unblock(block_id)
            except Exception:  # noqa: BLE001
                pass
        if undo.get("ryu_blocks"):
            notes.append(f"removed {len(undo['ryu_blocks'])} drop rule(s)")
        rest = {k: v for k, v in undo.items() if k in ("routes", "block")}
        if rest:
            notes.append(super().undo(intent, rest))
        return "; ".join(notes) or "nothing to undo"


class LiveVerifier(Verifier):
    def __init__(self, collector, service, controller, security=None):
        super().__init__(collector, service, security)
        self.controller = controller
        servers = sorted((d for d in controller.config.host_devices if d.kind == "server"), key=lambda d: d.name)
        self.probe_target = servers[0].ip.split("/")[0] if servers else None

    def _blocked(self, subject: str) -> Tuple[bool, str]:
        net = self.controller._net
        if net is None or self.probe_target is None:
            return False, "no live network"
        out = net.get(subject).cmd(f"ping -n -q -c 3 -i 0.2 -W 1 {self.probe_target}")
        m = re.search(r"(\d+) received", out)
        received = int(m.group(1)) if m else 0
        return received == 0, f"ping {subject} -> {self.probe_target}: {received}/3 answered"
