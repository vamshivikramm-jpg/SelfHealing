"""The full closed loop on one virtual clock (Module 7).

After every telemetry collection of a :class:`ScenarioRunner`:

    traffic delivery (ServiceModel) -> Zero Trust step -> DecisionEngine step
    -> HealingEngine step (execute / verify / undo / escalate / fail back)

This is the research document's Algorithm 1 running end to end in
simulation. ``heal=False`` keeps detection and decisions but executes
nothing (the "detect and alert" baseline).
"""

from __future__ import annotations

from network.decision.evaluation import IntelligenceLoop
from network.decision.graph import NetworkGraph
from network.healing.engine import HealingEngine, HealingPolicy
from network.healing.executor import SimulatedExecutor
from network.healing.service import ServiceModel
from network.healing.verify import Verifier


class ClosedLoop(IntelligenceLoop):
    def __init__(self, runner, registry, zt_policy, decision_policy, topology, security_plan, predictor=None,
                 healing_policy: HealingPolicy = HealingPolicy(), heal: bool = True):
        self.service = ServiceModel(NetworkGraph.from_topology(topology), lambda: runner.builder.net,
                                    runner.collector.store)
        super().__init__(runner, registry, zt_policy, decision_policy, topology, security_plan, predictor)
        self.healing = None
        if heal:
            executor = SimulatedExecutor(runner.controller, runner.manager, self.service, self.security)
            verifier = Verifier(runner.collector, self.service, self.security)
            self.healing = HealingEngine(self.decisions, executor, verifier, healing_policy)

    def on_collection(self, result) -> None:
        self.service.step(result.timestamp)
        super().on_collection(result)
        if self.healing is not None:
            self.healing.step(result.timestamp)
