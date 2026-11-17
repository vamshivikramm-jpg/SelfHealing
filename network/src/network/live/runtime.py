"""The closed loop on the live lab, for the dashboard (Module 8, live P6).

:class:`LiveRuntime` is the simulation runtime with every back-end
replaced by its live counterpart:

| Concern | Simulation | Live |
|---|---|---|
| network | in-memory data plane | Mininet + OVS, the self-healing Ryu app |
| failures | FailureManager on the simulated net | the same FailureManager on Mininet (``tc``, links, OVS bridges) |
| physical damage | ``SimulatedNet.break_*`` | :class:`LiveFaults` (all packets dropped, controller not told) |
| telemetry | ``SimulatedSource`` | :class:`LiveSource` (active probes, OpenFlow counters) |
| traffic delivery | modelled | :class:`LiveServiceModel` (real pings) |
| Zero Trust inputs | generated flows and logins | :class:`LiveSecurityFeed` (switch flow counters, server login logs) |
| Zero Trust enforcement | in-memory rules | :class:`LiveEnforcer` (rules pushed to Ryu, decided per flow) |
| attacks | synthetic records | :class:`LiveTraffic` (real programs in the hosts) |
| healing actions | in-memory routes/blocks | :class:`LiveExecutor` (pinned paths, drop rules) |
| prediction | z-score trained on simulated runs | z-score trained on the live warm-up |

Operator actions (inject failure, launch attack, demo, autonomy,
approve/reject) keep the simulation runtime's API. Failures and attacks are
refused while the predictor learns normal traffic (first ~3 minutes).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import List, Optional

from network.analytics.recorder import TelemetryRecorder
from network.analytics.repository import TelemetryRepository
from network.analytics.scenarios import ScenarioRunner, _quiet_logger, _VirtualClock
from network.control import NetworkController
from network.decision.graph import NetworkGraph
from network.failure.failure_manager import FailureManager
from network.healing.loop import ClosedLoop
from network.live.executor import LiveExecutor, LiveVerifier
from network.live.faults import LiveFaults
from network.live.prediction import LivePredictorTrainer
from network.live.ryu_client import RyuClient
from network.live.security import LiveSecurityFeed, LiveTraffic, attach_live_enforcer
from network.live.service import LiveServiceModel
from network.live.source import LiveSource
from network.simulation.runtime import CONFIG_DIR, RuntimeError_, SimulationRuntime
from network.telemetry.collector import TelemetryCollector


class LiveScenarioRunner(ScenarioRunner):
    """``ScenarioRunner``'s failure injection (``_inject``/``_recover``/
    precursors) on the real network. ``builder.net`` is the physical-fault
    emulator, which has the simulated data plane's ``break_*``/``repair_*``."""

    def __init__(self, plan, config, config_path: str, client: RyuClient, start_time: float):
        self.plan = plan
        self.config = config
        self.repository = TelemetryRepository()
        self.start_time = start_time
        self.clock = _VirtualClock(start_time)
        self.controller = NetworkController(config_path=config_path, logger=_quiet_logger("controller"))
        self.faults = LiveFaults(self.controller)
        self.builder = SimpleNamespace(net=self.faults)
        self.manager = FailureManager(self.controller, logger=_quiet_logger("failures"))
        self.collector = TelemetryCollector([LiveSource(self.controller, client)],
                                            event_bus=self.controller.event_bus, interval_s=plan.step_s,
                                            clock=self.clock, logger=_quiet_logger("collector"))
        self.recorder = TelemetryRecorder(self.repository, collector=self.collector,
                                          event_bus=self.controller.event_bus, logger=_quiet_logger("recorder"))
        self._health_changes: List[dict] = []

    def run(self):  # pragma: no cover - the live runtime drives the clock itself
        raise NotImplementedError("the live network runs on the wall clock (LiveRuntime)")


class _FeedTraffic:
    """Presents the live feed where the loop expects a TrafficGenerator."""

    def __init__(self, feed: LiveSecurityFeed):
        self.feed = feed

    def step(self, t: float, ts: float):
        return self.feed.step(ts)


class LiveRuntime(SimulationRuntime):
    mode = "live"
    simulated = False

    def __init__(self, config_dir: str = CONFIG_DIR, interval_s: float = 2.0,
                 topology_file: str = "enterprise-redundant.yaml", warmup_s: float = 120.0,
                 client: Optional[RyuClient] = None, use_predictor: bool = True, start_traffic: bool = True):
        self.client = client or RyuClient()
        self.warmup_s = float(warmup_s)
        self.start_traffic = start_traffic
        self.trainer: Optional[LivePredictorTrainer] = None
        self._use_live_predictor = use_predictor
        super().__init__(config_dir, interval_s=interval_s, use_predictor=False, topology_file=topology_file)
        if use_predictor:
            self.trainer = LivePredictorTrainer(self.collector, self._predictor_ready, warmup_s=self.warmup_s,
                                                logger=_quiet_logger("runtime.predictor")).attach()

    # -- back-ends -----------------------------------------------------------------------

    def _make_runner(self, plan) -> LiveScenarioRunner:
        return LiveScenarioRunner(plan, self.topology, os.path.join(self.config_dir, self.topology_file),
                                  self.client, self.started_at)

    def _make_loop(self, zt_policy, decision_policy, security_plan) -> ClosedLoop:
        runner, client = self.runner, self.client
        service = LiveServiceModel(NetworkGraph.from_topology(self.topology), runner.controller, client,
                                   runner.collector.store)
        loop = ClosedLoop(
            runner, self.registry, zt_policy, decision_policy, self.topology, security_plan, None,
            service=service,
            executor_factory=lambda l: LiveExecutor(runner.controller, runner.manager, l.service, client,
                                                    l.security),
            verifier_factory=lambda l: LiveVerifier(runner.collector, l.service, runner.controller, l.security))
        self.hosts = LiveTraffic(runner.controller, self.registry, self.config_dir, self.topology_file)
        self.feed = LiveSecurityFeed(client, self.registry, self.hosts.auth_logs)
        loop.traffic = _FeedTraffic(self.feed)
        self.enforcer = attach_live_enforcer(loop.security, client)
        return loop

    def reset_controller_state(self) -> dict:
        """Remove every block and pinned path in the Ryu app. They belong to
        healing actions of an earlier session (the controller outlives the
        dashboard) and would otherwise keep devices cut off."""
        removed = {"blocks": 0, "pins": 0}
        for block in self.client.blocks() or []:
            self.client.unblock(block["id"])
            removed["blocks"] += 1
        for ip, info in (self.client.routes() or {}).items():
            if info.get("pinned"):
                self.client.unpin(ip)
                removed["pins"] += 1
        return removed

    def _start_network(self) -> None:
        self.client.set_topology(self.topology_file)
        self.cleared_at_start = self.reset_controller_state()
        self.controller.start_network()
        net = self.controller._net
        deadline = time.time() + 45
        while time.time() < deadline and not all(s.connected() for s in net.switches):
            time.sleep(0.5)
        for host in net.hosts:  # every host talks once, so the controller locates it
            host.cmd(f"ping -c1 -W1 {self.registry.get(host.name).gateway}")
        if self.start_traffic:
            self.hosts.start()

    def _predictor_ready(self, predictor) -> None:
        with self._lock:
            self.predictor = predictor
            self.loop.predictor = predictor
        self.predictor_info = {"model": "zscore", "threshold": predictor.threshold,
                               "trained_on": "live warm-up telemetry", "simulated": False}

    # -- operator actions --------------------------------------------------------------------

    @property
    def learning(self) -> bool:
        return self.trainer is not None and self.trainer.predictor is None and self.trainer.error is None

    def _refuse_while_learning(self) -> None:
        if self.learning:
            left = self.trainer.remaining_s
            raise RuntimeError_(f"the predictor is still learning normal traffic (~{left:.0f} s left); "
                                "failures and attacks would be learnt as normal")

    def inject_failure(self, *args, **kwargs) -> dict:
        self._refuse_while_learning()
        return super().inject_failure(*args, **kwargs)

    def launch_attack(self, kind: str, attacker: str, duration_s: float = 60.0, target: Optional[str] = None,
                      victim: Optional[str] = None) -> dict:
        self._refuse_while_learning()
        from network.security.traffic import ATTACK_KINDS

        if kind not in ATTACK_KINDS:
            raise RuntimeError_(f"unknown attack kind {kind!r}; use one of {ATTACK_KINDS}")
        identity = self.registry.get(attacker)
        if identity is None or identity.kind != "host":
            raise RuntimeError_("the attacker must be a host (h1-h5)")
        if not 5.0 <= float(duration_s) <= 3600.0:
            raise RuntimeError_("duration_s must be between 5 and 3600")
        if kind == "spoofing":
            victim = victim or next(i.name for i in self.registry.with_role("admin") if i.name != attacker)
        with self._lock:
            record = self.hosts.launch_attack(kind, attacker, float(duration_s), target, victim)
            item = {"id": f"{kind}-{len(self.injections) + 1}", "type": "attack", "kind": kind,
                    "target": [attacker], "victim": record["victim"], "attack_target": record["target"],
                    "start": record["started"], "end": record["started"] + float(duration_s), "state": "active",
                    "real": True}
            self.injections.append(item)
            return self._public(item)

    def start_demo(self) -> List[dict]:
        self._refuse_while_learning()
        return super().start_demo()

    def shutdown(self) -> None:
        """Stop the loop, the host programs and the network; switch Zero Trust off."""
        self.stop()
        try:
            self.hosts.stop()
            self.client.request("DELETE", "/sh/zt")
            self.reset_controller_state()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.controller.stop_network()
        finally:
            from mininet.clean import cleanup

            cleanup()

    # -- views -----------------------------------------------------------------------------------

    def status(self) -> dict:
        info = super().status()
        info["learning"] = self.learning
        info["predictor"] = self.predictor_info or (self.trainer.status() if self.trainer else None)
        info["warmup_remaining_s"] = self.trainer.remaining_s if self.trainer else 0.0
        info["zero_trust"] = {"pushes": self.enforcer.pushes, "error": self.enforcer.last_error,
                              "feed_error": self.feed.last_error}
        try:
            info["controller"] = self.client.health()
        except Exception as exc:  # noqa: BLE001
            info["controller"] = {"ok": False, "error": str(exc)}
        return info
