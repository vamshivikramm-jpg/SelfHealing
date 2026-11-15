"""Background Zero Trust simulation for the viewer (Submodule 5.8).

Generates SIMULATED traffic (and, if enabled, SYNTHETIC attacks) every
``interval_s`` seconds of wall-clock time and runs it through a
:class:`ZeroTrustEngine`, so the dashboard API has live trust scores and
security events before real flow telemetry exists.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from network.security.engine import ZeroTrustEngine
from network.security.traffic import SecurityPlan, TrafficGenerator, generate_security_plan

DAY_S = 86400.0


class LiveSecuritySimulation:
    def __init__(self, registry, policy, event_bus=None, interval_s: float = 5.0, attacks: bool = False,
                 seed: int = 0, clock=time.time):
        self.registry = registry
        self.policy = policy
        self.interval_s = float(interval_s)
        self.attacks = attacks
        if attacks:
            self.plan = generate_security_plan(registry, DAY_S, seed=seed, n_attacks=40, step_s=interval_s,
                                               warmup_s=120.0, gap_s=(180.0, 600.0))
        else:
            self.plan = SecurityPlan(duration_s=DAY_S, step_s=interval_s, seed=seed)
        self.engine = ZeroTrustEngine(registry, policy, event_bus)
        self.generator = TrafficGenerator(registry, self.plan)
        self._clock = clock
        self._step = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> None:
        t = (self._step * self.interval_s) % self.plan.duration_s
        flows, auth = self.generator.step(t, self._clock())
        self.engine.step(self._clock(), flows, auth)
        self._step += 1

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zero-trust-sim", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the simulation alive
                pass
            self._stop.wait(self.interval_s)

    def status(self) -> dict:
        info = self.engine.status()
        info.update({"running": self.running, "simulated": True, "synthetic_attacks": self.attacks,
                     "interval_s": self.interval_s})
        return info
