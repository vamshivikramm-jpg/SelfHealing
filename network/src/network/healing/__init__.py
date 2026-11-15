"""Autonomous self-healing (Module 7).

- ``service``   installed routes + per-demand delivery (user-visible view)
- ``executor``  intents -> actions on the simulated network, and undo
- ``verify``    post-condition checks against telemetry and delivery
- ``engine``    HealingEngine: execute, verify, undo/retry, escalate, fail back
- ``loop``      the full closed loop (telemetry -> prediction -> trust ->
                decisions -> healing) on one clock
- ``evaluation``  CLI: ``python -m network.healing.evaluation``

Execution is SIMULATED; a live executor needs the Linux lab.
"""
