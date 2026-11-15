"""Intelligent decision engine (Module 6).

- ``policy``      config/decision_policy.yaml (autonomy, Eq. 4 weights, limits)
- ``graph``       network graph, Eq. 6 path cost, constrained shortest paths
- ``risk``        Eq. 4 combined risk per link/node
- ``intents``     vendor-neutral actions with post-conditions (for Module 7)
- ``strategies``  which intents a situation calls for
- ``safety``      conflicts, de-duplication, cooldown, human approval gate
- ``engine``      DecisionEngine: one step = assess, decide, gate, log
- ``evaluation``  CLI: ``python -m network.decision.evaluation``
- ``live``        decision loop attached to a telemetry collector (viewer)

Decides only; Module 7 executes and verifies. Standard library + PyYAML.
"""
