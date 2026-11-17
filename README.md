# AI-Powered Autonomous Zero Trust Self-Healing Network

Research-grade virtual enterprise network with autonomous failure prediction,
zero-trust scoring, and self-healing — built one module at a time.

Current state: **Module 1 complete** — Enterprise SDN Foundation with
configurable topology, network control layer, failure injection framework
(models, registry, manager, scenarios), and structured event system.
Version **1.0.0**. **Module 2 (telemetry and monitoring)**: the
telemetry pipeline runs with a *simulated* source; live sources need a
Linux lab with Mininet. **Module 3 (data management and analytics)**:
SQLite history, preprocessing, features, baselines, labelled scenario
datasets and availability/MTTR summaries — all on simulated data.
**Module 4 (AI failure prediction)**: baselines and ML models compared on
simulated runs with designed warning patterns, model registry and live
early warning. **Module 5 (Zero Trust)**: device identities, per-request
default-deny policy, dynamic trust scores, behaviour detectors and
simulated enforcement, evaluated on synthetic attacks. **Module 6
(decision engine)**: Eq. 4 risk, vendor-neutral intents, path planning,
safety checks and a human approval gate, evaluated end to end in
simulation. **Module 7 (self-healing)**: executes decisions, verifies
them against telemetry and traffic delivery, undoes/escalates, evaluated
against baselines and ablations — all on a simulated network with a
physical-fault model. **Module 8 (dashboard)**: tabbed live dashboard
with a simulation mode that runs the whole closed loop and lets you inject
failures and attacks, approve decisions and change autonomy. **Module 9
(evaluation)**: reproducible 10-seed experiments with statistics, results in
[`docs/results.md`](docs/results.md), setup in
[`docs/deployment.md`](docs/deployment.md). **Live lab**: the whole closed
loop also runs on the real emulated network (Mininet + Open vSwitch + the
self-healing Ryu app on a Linux VM), with measured telemetry, real traffic
and attacks, Zero Trust enforced by the switches, and the dashboard served
from the VM.

### Dashboard

Two ways to run it:

| | Simulation (any Mac/Linux) | Live lab (Linux VM with Docker) |
|---|---|---|
| Start | `VIEWER_MODE=simulation python3 -m uvicorn viewer.app:app --host 127.0.0.1 --port 8000` | on the VM: `docker compose up -d && make live-dashboard` |
| Open | http://127.0.0.1:8000 | http://&lt;vm-ip&gt;:8000 |
| Network | simulated data plane | Mininet + OVS + Ryu |
| Failures / attacks | synthetic | change the emulated network / real programs in the hosts |
| Ready after | ~15 s (predictor trained on simulated runs) | ~20 s; failures and attacks after ~2.5 min (predictor learns live traffic) |

**Login**: the dashboard asks for a user name and password — `root` /
`toor` by default (set `VIEWER_USER` / `VIEWER_PASSWORD` to change them).

**Shortcuts** (no more killing the port by hand): `scripts/SelfHealingStart`
frees port 8000, starts the dashboard in the background, waits until it is
ready and opens the browser; `scripts/SelfHealingStop` stops it. Add `live`
for the live lab on the VM (`SelfHealingStart live`, `SelfHealingStop live`,
`SelfHealingStop all`). Make them commands anywhere with
`ln -sf "$PWD"/scripts/SelfHealing{Start,Stop} ~/.local/bin/` (any directory
on your `PATH`). The VM defaults to `vamshi@172.16.120.137`; override with
`SELFHEALING_VM=user@host`.

Simulation mode needs the `mininet` Python package (imports only), numpy
and scikit-learn — the macOS system `python3` has them; the project
`.venv` does not, and there the dashboard falls back to the read-only
offline preview. Live lab setup: [`docs/deployment.md`](docs/deployment.md).

## Stack

| Component | Where | Status |
|---|---|---|
| Mininet + Open vSwitch data plane | `network/` | running (bootstrap + topology) |
| Ryu SDN controller | `controller/` | running (self-healing app: loop-safe routing, proxy ARP, blocks, stats, REST API) |
| Enterprise topology generator | `network/src/network/` | Submodule 1.2 complete |
| Network Control Layer | `network/src/network/control/` | Submodule 1.3 complete |
| Failure Models & Registry | `network/src/network/failure/` | Submodule 1.4A complete |
| FailureManager (router/switch/link/impairment) | `network/src/network/failure/` | Submodules 1.4B + 1.4C complete |
| NetworkController impairment APIs | `network/src/network/control/` | Submodule 1.4C.1 complete |
| Failure Scenarios | `network/src/network/failure/` | Submodule 1.4D complete |
| Telemetry pipeline (simulated source) | `network/src/network/telemetry/` | Module 2 — simulated only, live sources pending |
| Read-only viewer + telemetry API | `viewer/` | basic; full dashboard is Module 8 |
| Data management & analytics (SQLite, features, baselines, datasets) | `network/src/network/analytics/` | Module 3 — on simulated data |
| Failure prediction (models, registry, live early warning) | `network/src/network/prediction/` | Module 4 — simulated data with synthetic warning patterns |
| Zero Trust (identities, trust, policy, enforcement) | `network/src/network/security/` | Module 5 — simulated traffic, synthetic attacks; enforcement simulated |
| Decision engine (risk, intents, paths, safety) | `network/src/network/decision/` | Module 6 — decides only; evaluated in simulation |
| Self-healing (execute, verify, undo, escalate) | `network/src/network/healing/` | Module 7 — simulated execution; live executor needs the Linux lab |
| Dashboard + simulation runtime | `viewer/`, `network/src/network/simulation/runtime.py` | Module 8 — simulation mode with operator controls; read-only otherwise |

## Prerequisites

- Ubuntu Linux host (22.04 or 24.04) with Docker Engine + Compose v2
- OVS kernel module on the host: `sudo modprobe openvswitch`
- `python3` + `pytest` on the host for bootstrap unit tests (`python3 -m pip install pytest`)

## Quickstart

```bash
docker compose up -d --build     # start mininet-net + ryu-controller
docker compose ps                # both services should be healthy
make net-up                      # build + start the enterprise network
make net-status                  # show OVS bridges/ports
```

## Test

```bash
make config    # validate docker-compose.yml
make unit      # bootstrap unit tests (host)
make smoke     # container smoke test (builds the stack)
make net-test  # all unit + integration tests in-container (Modules 1.1-1.4)
```

## Manage the enterprise network

All commands go through `NetworkController` (the single interface to Mininet):

```bash
make net-up       # build/start the network (start_network)
make net-down     # stop and clean all Mininet artifacts (stop_network)
make net-reset    # clean, rebuild, start (reset_network)
make net-status   # report bridges/ports (network_status)
```

## Layout

```
network/       Mininet + OVS container + topology generator (src/, config/)
controller/    Ryu OpenFlow controller container (apps/)
tests/         unit + integration + smoke tests
docs/          architecture blueprint + research contribution
```

See `docs/README.md` for the full blueprint and the development roadmap.
