"""Live simulation runtime for the dashboard (Submodule 8.1).

Runs the full closed loop of Modules 2-7 on the *wall clock*, on the
redundant simulation topology:

    telemetry -> failure prediction -> Zero Trust -> decisions -> self-healing

and lets an operator inject failures (optionally physical, optionally
preceded by a warning ramp) and attacks while it runs, approve or reject
pending decisions, and change the autonomy level. Everything it reports
is SIMULATED.

It reuses the Module 3 ``ScenarioRunner`` for the network, failure
manager and collector (never calling its ``run()``), and Module 7's
``ClosedLoop`` for the intelligence layer. Requires the ``mininet``
Python package (imports only) and, for prediction, numpy.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

from network.analytics.recorder import TelemetryRecorder
from network.analytics.repository import TelemetryRepository
from network.analytics.scenarios import (
    COMPATIBLE_PRECURSORS,
    DEFAULT_PRECURSOR_PEAK,
    FAILURE_KINDS,
    NODE_KINDS,
    PlannedFailure,
    Precursor,
    ScenarioPlan,
    ScenarioRunner,
    _quiet_logger,
)
from network.config import load_topology_config
from network.decision.graph import path_links
from network.decision.policy import AUTONOMY_LEVELS, DecisionPolicy
from network.healing.loop import ClosedLoop
from network.security.identity import IdentityRegistry
from network.security.policy import ZeroTrustPolicy
from network.security.traffic import ATTACK_KINDS, AttackSpec, SecurityPlan

CONFIG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "config"))
_ids = itertools.count(1)

# A scripted demonstration: seconds from "start demo", what happens.
DEMO_SCRIPT = (
    (10, "failure", {"kind": "link", "target": ["sw-a", "sw-b"], "duration_s": 60, "physical": True}),
    (100, "failure", {"kind": "switch", "target": ["sw-b"], "duration_s": 60}),
    (180, "failure", {"kind": "latency", "target": ["sw-a", "sw-b"], "duration_s": 60, "magnitude": 150,
                      "warning_s": 60}),
    (300, "attack", {"kind": "flood", "attacker": "h5", "duration_s": 60}),
    (390, "attack", {"kind": "spoofing", "attacker": "h4", "duration_s": 45}),
)


class RuntimeError_(ValueError):
    """Invalid operator request (bad kind, target, duration...)."""


class SimulationRuntime:
    def __init__(self, config_dir: str = CONFIG_DIR, interval_s: float = 2.0, use_predictor: bool = True,
                 seed: int = 0, clock: Callable[[], float] = time.time, predictor=None):
        self.config_dir = config_dir
        self.interval_s = float(interval_s)
        self._clock = clock
        self.topology = load_topology_config(os.path.join(config_dir, "enterprise-redundant.yaml"))
        self.registry = IdentityRegistry.from_config(self.topology, os.path.join(config_dir, "identities.yaml"))
        zt_policy = ZeroTrustPolicy.from_file(os.path.join(config_dir, "zero_trust_policy.yaml"))
        decision_policy = DecisionPolicy.from_file(os.path.join(config_dir, "decision_policy.yaml"))

        self.started_at = clock()
        plan = ScenarioPlan(duration_s=10 * 365 * 86400.0, step_s=self.interval_s, seed=seed)
        self.runner = ScenarioRunner(plan, config=self.topology, start_time=self.started_at)
        self.runner.collector.interval_s = self.interval_s
        self.controller = self.runner.controller
        self.manager = self.runner.manager
        self.collector = self.runner.collector
        self.bus = self.controller.event_bus
        self.repository = TelemetryRepository()
        self.recorder = TelemetryRecorder(self.repository, collector=self.collector, event_bus=self.bus,
                                          logger=_quiet_logger("runtime.recorder"))

        self.predictor = predictor
        self.predictor_info = None
        if predictor is None and use_predictor:
            self.predictor = self._train_predictor()

        security_plan = SecurityPlan(duration_s=plan.duration_s, step_s=self.interval_s, seed=seed)
        self.loop = ClosedLoop(self.runner, self.registry, zt_policy, decision_policy, self.topology,
                               security_plan, self.predictor)
        for engine in (self.loop.security, self.loop.decisions, self.loop.healing):
            engine.event_bus = self.bus
        self.service = self.loop.service
        self.security = self.loop.security
        self.decisions = self.loop.decisions
        self.healing = self.loop.healing

        self.events: Deque[dict] = deque(maxlen=3000)
        self.bus.subscribe(self._on_event)
        self.injections: List[dict] = []
        self._demo: List[tuple] = []
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.ticks = 0
        self.recorder.attach()
        self.controller.start_network()

    # -- setup ---------------------------------------------------------------------

    def _train_predictor(self):
        from network.decision.evaluation import train_predictor
        from network.prediction.online import OnlinePredictor

        model, threshold = train_predictor(self.topology)  # two 2 h simulated runs, ~15 s
        self.predictor_info = {"model": "zscore", "threshold": threshold, "trained_on": "2 simulated runs"}
        return OnlinePredictor(model, threshold, self.collector, step_s=5.0,
                               provenance={"simulated": True, "synthetic_precursors": True},
                               model_label="zscore (trained at startup)",
                               logger=_quiet_logger("runtime.predictor")).attach()

    # -- clock / loop -------------------------------------------------------------

    @property
    def now(self) -> float:
        return self._clock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> None:
        with self._lock:
            ts = self.now
            self._run_demo(ts)
            self._advance_injections(ts)
            self.runner.clock.now = ts
            self.collector.collect_once()
            self.ticks += 1

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="simulation-runtime", daemon=True)
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
            except Exception:  # noqa: BLE001 - keep the runtime alive
                pass
            self._stop.wait(self.interval_s)

    # -- operator actions ------------------------------------------------------------

    def inject_failure(self, kind: str, target: List[str], duration_s: float = 60.0, physical: bool = False,
                       magnitude: Optional[float] = None, warning_s: float = 0.0) -> dict:
        if kind not in FAILURE_KINDS:
            raise RuntimeError_(f"unknown failure kind {kind!r}; use one of {FAILURE_KINDS}")
        target = tuple(target)
        names = {d.name for d in self.topology.devices}
        if any(t not in names for t in target):
            raise RuntimeError_(f"unknown device in {target}")
        if kind in NODE_KINDS:
            expected = "router" if kind == "router" else "switch"
            if len(target) != 1 or self._kind(target[0]) != expected:
                raise RuntimeError_(f"a {kind} failure needs one {expected}")
        elif len(target) != 2 or self.controller.state.get_link(*target) is None:
            raise RuntimeError_(f"{target} is not a link")
        if not 5.0 <= float(duration_s) <= 3600.0:
            raise RuntimeError_("duration_s must be between 5 and 3600")
        if magnitude is None and kind not in NODE_KINDS + ("link",):
            magnitude = {"latency": 150.0, "packet_loss": 20.0, "bandwidth": 5.0}[kind]
        with self._lock:
            if any(i["state"] in ("scheduled", "warning", "active") and self._overlaps(i["target"], target)
                   for i in self.injections if i["type"] == "failure"):
                raise RuntimeError_(f"{'-'.join(target)} already has a failure in progress")
            now = self.now - self.started_at
            precursor = None
            if warning_s and warning_s > 0:
                p_kind = COMPATIBLE_PRECURSORS[kind][0]
                link = target if len(target) == 2 else self._a_link_of(target[0])
                precursor = Precursor(p_kind, float(warning_s), DEFAULT_PRECURSOR_PEAK[p_kind], link)
            start = now + (float(warning_s) if precursor else 0.0) + 0.01
            failure = PlannedFailure(kind, target, start, float(duration_s), magnitude, precursor, bool(physical))
            item = {"id": f"inj-{next(_ids)}", "type": "failure", "kind": kind, "target": list(target),
                    "physical": bool(physical), "magnitude": magnitude, "warning_s": float(warning_s or 0),
                    "start": self.started_at + failure.window_start_s, "failure_start": self.started_at + start,
                    "end": self.started_at + failure.end_s, "state": "scheduled", "_failure": failure}
            self.injections.append(item)
            return self._public(item)

    def launch_attack(self, kind: str, attacker: str, duration_s: float = 60.0, target: Optional[str] = None,
                      victim: Optional[str] = None) -> dict:
        if kind not in ATTACK_KINDS:
            raise RuntimeError_(f"unknown attack kind {kind!r}; use one of {ATTACK_KINDS}")
        identity = self.registry.get(attacker)
        if identity is None or identity.kind != "host":
            raise RuntimeError_("the attacker must be a host (h1-h5)")
        if not 5.0 <= float(duration_s) <= 3600.0:
            raise RuntimeError_("duration_s must be between 5 and 3600")
        web = self.registry.with_role("web_server")[0].name
        db = self.registry.with_role("db_server")[0].name
        if kind == "spoofing":
            victim = victim or next(i.name for i in self.registry.with_role("admin") if i.name != attacker)
            target = db
        target = target or (web if kind == "flood" else db)
        with self._lock:
            now = self.now - self.started_at
            spec = AttackSpec(kind, attacker, now + 0.01, float(duration_s), target, victim, f"{kind}-{next(_ids)}")
            gen = self.loop.traffic
            gen.plan = dataclasses.replace(gen.plan, attacks=gen.plan.attacks + (spec,))
            item = {"id": spec.attack_id, "type": "attack", "kind": kind, "target": [attacker], "victim": victim,
                    "attack_target": target, "start": self.started_at + spec.start_s,
                    "end": self.started_at + spec.end_s, "state": "active"}
            self.injections.append(item)
            return self._public(item)

    def start_demo(self) -> List[dict]:
        with self._lock:
            base = self.now
            self._demo = [(base + offset, what, dict(args)) for offset, what, args in DEMO_SCRIPT]
            return [{"at": at, "what": what, **args} for at, what, args in self._demo]

    def set_autonomy(self, level: str) -> str:
        if level not in AUTONOMY_LEVELS:
            raise RuntimeError_(f"autonomy must be one of {AUTONOMY_LEVELS}")
        with self._lock:
            policy = dataclasses.replace(self.decisions.policy, autonomy=level)
            self.decisions.policy = policy
            self.decisions.guard.policy = policy
        return level

    def approve(self, intent_id: str) -> dict:
        with self._lock:
            return self.decisions.approve(intent_id).to_dict()

    def reject(self, intent_id: str) -> dict:
        with self._lock:
            return self.decisions.reject(intent_id).to_dict()

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _overlaps(a, b) -> bool:
        """Same link, same node, or a node and a link attached to it."""
        a, b = tuple(a), tuple(b)
        if len(a) == 2 and len(b) == 2:
            return set(a) == set(b)
        if len(a) == 1 and len(b) == 1:
            return a == b
        node, link = (a, b) if len(a) == 1 else (b, a)
        return node[0] in link

    def _kind(self, name: str) -> Optional[str]:
        return next((d.kind for d in self.topology.devices if d.name == name), None)

    def _a_link_of(self, node: str):
        link = next(l for l in self.topology.links if node in (l.src, l.dst) and
                    self._kind(l.dst if l.src == node else l.src) in ("switch", "router"))
        return (link.src, link.dst)

    def _run_demo(self, ts: float) -> None:
        due = [d for d in self._demo if d[0] <= ts]
        self._demo = [d for d in self._demo if d[0] > ts]
        for _, what, args in due:
            try:
                if what == "failure":
                    self.inject_failure(**args)
                else:
                    self.launch_attack(**args)
            except RuntimeError_:
                pass

    def _advance_injections(self, ts: float) -> None:
        t = ts - self.started_at
        for item in self.injections:
            if item["type"] == "attack":
                item["state"] = "active" if ts < item["end"] else "ended"
                continue
            f: PlannedFailure = item["_failure"]
            p = f.precursor
            if item["state"] in ("scheduled", "warning") and p is not None and f.window_start_s <= t < f.start_s:
                item["state"] = "warning"
                self.runner._set_precursor(p, min(1.0, (t - f.window_start_s + self.interval_s) / p.duration_s))
            if item["state"] in ("scheduled", "warning") and t >= f.start_s:
                self.runner._inject(f)
                item["state"] = "active"
            if item["state"] == "active" and t >= f.end_s:
                self.runner._recover(f)
                if p is not None:
                    self.runner._clear_precursor(p)
                item["state"] = "repaired"

    def _on_event(self, event) -> None:
        if event.type.value == "TELEMETRY_COLLECTED":
            return
        self.events.append({"type": event.type.value, "source": event.source, "time": event.timestamp,
                            "result": event.result, "data": event.data})

    @staticmethod
    def _public(item: dict) -> dict:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    # -- views ---------------------------------------------------------------------------

    def injections_view(self) -> List[dict]:
        with self._lock:
            return [self._public(i) for i in reversed(self.injections[-50:])]

    def rerouted(self) -> List[dict]:
        out = []
        for pair, path in self.service.routes.items():
            if path != self.service.baseline.get(pair):
                out.append({"src": pair[0], "dst": pair[1], "path": list(path), "links": path_links(path)})
        return out

    def status(self) -> dict:
        return {
            "mode": "simulation",
            "simulated": True,
            "running": self.running,
            "started_at": self.started_at,
            "uptime_s": self.now - self.started_at,
            "interval_s": self.interval_s,
            "ticks": self.ticks,
            "topology": self.topology.name,
            "autonomy": self.decisions.policy.autonomy,
            "predictor": self.predictor_info,
            "demo_pending": len(self._demo),
        }
