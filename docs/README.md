# Docs Index — AI-Powered Autonomous Zero Trust Self-Healing Network

Design and research documentation for the project. Implemented one module at a time; see the development roadmap for ordering.

## Documents

| Document | Description |
|---|---|
| [Project-Overview.pdf](Project-Overview.pdf) | 15-page plain-language overview for the team: what, how, every algorithm and why, the flow (source: [overview/project-overview.html](overview/project-overview.html)) |
| [results.md](results.md) | Results chapter: 10-seed experiments, statistics, figures, threats to validity |
| [deployment.md](deployment.md) | Setup (simulation and Linux lab), every command, settings, CI, troubleshooting |
| [demo-script.md](demo-script.md) | ~15 minute presentation walkthrough of the dashboard |
| [research-contribution.md](research-contribution.md) | Novelty, mathematical model, metrics and evaluation methodology (basis of the paper and report) |
| [results/full-run/](results/full-run/report.md) | Archived raw data, auto-generated report and figures of the full experiment run |
| [../network/README.md](../network/README.md) | Per-module technical documentation (Modules 1–9) |

Planned blueprint documents that were listed here originally but never
written (architecture, modules, database schema, API design, class and
sequence diagrams, Docker deployment, development roadmap) are covered for
now by `network/README.md`, `deployment.md` and the module docstrings.

## Status

- **Module 1 (Virtual Enterprise Network): COMPLETE** — Version 1.0.0. Includes configurable enterprise topology (1.2), Network Control Layer with `NetworkController`, `NetworkState`, in-process `EventBus` (13 event types), safety exceptions, structured JSON logging (1.3), Failure Injection Framework with 6 typed failure classes, `FailureRegistry`, `FailureManager` for router/switch/link/impairment injection and recovery (1.4A–1.4C), and reusable failure scenarios (1.4D). 219 unit tests passing. No AI, telemetry, trust, or healing logic.
- **Module 2 (Network Telemetry & Monitoring): IN PROGRESS** — telemetry data model, in-memory store, counter-to-rate derivation, threshold health evaluation, `TelemetryCollector` and viewer telemetry API, all running on a deterministic **simulated** source driven by `NetworkState` and injected failures. Live sources (Ryu OpenFlow port stats, ping/iperf probes) await the Linux lab (Module 2.0).
- **Module 3 (Network Data Management & Analytics): IMPLEMENTED ON SIMULATED DATA** — SQLite telemetry/event/baseline storage, recorder, resampling and preprocessing, rolling-window features, behavioural baselines, scripted failure scenarios on an in-memory data plane, labelled CSV datasets (with optional *synthetic* precursor patterns, flagged in metadata), and availability/incident/MTTR summaries. Datasets are simulated; results on them must be reported as such.
- **Module 4 (AI-Based Failure Prediction): IMPLEMENTED ON SIMULATED DATA** — objectives fixed before modelling (O1: ≥80 % of failures with warning signs caught ≥30 s ahead at ≤1 false alarm/hour); dataset schedule leak found and removed; threshold, z-score, Isolation Forest, logistic regression, random forest and gradient boosting compared on separate train/validation/test runs; versioned model registry with champion/rollback; live `FAILURE_PREDICTED` early warnings. Results hold only for the designed (synthetic) warning patterns.
- **Module 5 (Zero Trust Security): IMPLEMENTED ON SIMULATED DATA** — identities with posture, default-deny per-request policy with min-trust rules, Eq. 2 trust with half-life decay and quarantine hysteresis, six behaviour detectors plus the Eq. 3 threat index, two-table switch rules applied by a simulated enforcer (OpenFlow rendering for Ryu untested), and §8.3 metrics on synthetic attacks (AUROC 0.94, FAR 0.78 %, FRR 0 % for non-attacking devices over 5 × 3 h runs).
- **Module 6 (Intelligent Decision Engine): IMPLEMENTED, EVALUATED IN SIMULATION** — Eq. 4 combined risk, NDO intents with post-conditions, Eq. 6 constrained shortest paths and load balancing, conflict/cooldown/approval safety checks (autonomy L1–L3), live decision loop. End-to-end evaluation (3 × 3 h, 54 failures and attacks, redundant simulation-only topology): every incident got the action the written rules expect, 0 missed, 0 unneeded actions/hour, p95 decision latency 0.33 ms. Decides only; execution is Module 7.
- **Module 7 (Autonomous Self-Healing): IMPLEMENTED, EVALUATED IN SIMULATION** — physical-fault model (software cannot fix a cut cable or dead switch; telemetry reads real simulated state), route-aware traffic-delivery model, executor with undo, post-condition verification from telemetry/delivery/enforcement probes, retry and escalation, fail-back, closed loop of Modules 2–7. Evaluation (3 × 3 h, 17 outages, 6 physical): MTTR 80 s → 23 s, user-visible loss 93 % → 32 %, every software-fixable outage healed before manual repair; without verification 6 failed repairs were falsely reported successful. No live executor yet.
- **Module 8 (Advanced Dashboard): IMPLEMENTED** — tabbed dashboard (live topology with health/reroute/prediction/quarantine overlays, telemetry charts, predictions, Zero Trust, decisions with approve/reject, healing with verification evidence, timeline, analytics) and a simulation mode that runs the full Modules 2–7 loop on the wall clock with operator controls (inject failures/attacks, autonomy, scripted demo). Controls exist only in simulation mode; the viewer is read-only otherwise (no login). Checked in a real browser.
- **Module 9 (Research Evaluation & Deployment): IMPLEMENTED** — reproducible experiment runner (fixed seeds, provenance, 10 runs per condition, 95 % CIs, paired Wilcoxon + Holm), baselines (a)–(d) and ablations (§9.2, §9.4), sensitivity and scalability, results chapter [`results.md`](results.md) with archived raw data, [`deployment.md`](deployment.md), [`demo-script.md`](demo-script.md), and CI (GitHub Actions, Python 3.9 + 3.11). All results are on the simulated network.
- Research contribution document: v1.0 drafted
