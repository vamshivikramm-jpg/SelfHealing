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
datasets and availability/MTTR summaries — all on simulated data. No AI,
decision, healing or trust logic yet. Inter-subnet routing is a later
module.

## Stack

| Component | Where | Status |
|---|---|---|
| Mininet + Open vSwitch data plane | `network/` | running (bootstrap + topology) |
| Ryu SDN controller | `controller/` | running (registry + L2 learning switch) |
| Enterprise topology generator | `network/src/network/` | Submodule 1.2 complete |
| Network Control Layer | `network/src/network/control/` | Submodule 1.3 complete |
| Failure Models & Registry | `network/src/network/failure/` | Submodule 1.4A complete |
| FailureManager (router/switch/link/impairment) | `network/src/network/failure/` | Submodules 1.4B + 1.4C complete |
| NetworkController impairment APIs | `network/src/network/control/` | Submodule 1.4C.1 complete |
| Failure Scenarios | `network/src/network/failure/` | Submodule 1.4D complete |
| Telemetry pipeline (simulated source) | `network/src/network/telemetry/` | Module 2 — simulated only, live sources pending |
| Read-only viewer + telemetry API | `viewer/` | basic; full dashboard is Module 8 |
| Data management & analytics (SQLite, features, baselines, datasets) | `network/src/network/analytics/` | Module 3 — on simulated data |
| AI / Decision / Healing / Trust | — | later modules |

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
