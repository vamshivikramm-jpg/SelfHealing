# network/ — Virtual Enterprise Network (Module 1)

Containerized Mininet + Open vSwitch data plane with a **configurable
enterprise topology generator** (Submodule 1.2), a **Network Control
Layer** (Submodule 1.3), the **Failure Injection Framework**
(Submodules 1.4A–1.4C), and **Reusable Failure Scenarios**
(Submodule 1.4D). Version **1.0.0**.

## What's here

```
Dockerfile            Ubuntu 24.04 + Open vSwitch + Mininet (apt) + this package
entrypoint.sh         OVS init; keeps the container alive
pyproject.toml        Python package (installable, console script `enterprise-net`)
src/network/
  config.py             YAML config loading + validation (ConfigError)
  devices.py            Device classes: Router, Switch, Host, Server
  enterprise_topology.py  Mininet Topo built from a validated config
  network_builder.py    Builds/start/stops the Mininet network (Ryu remote controller)
  control/              Network Control Layer (Submodule 1.3)
    network_controller.py  NetworkController — the ONLY interface to Mininet
    exceptions.py          Custom safety exceptions
  failure/              Failure Injection Framework (1.4A–1.4D)
    failure_types.py       Failure data model + enums
    failure_registry.py    In-memory FailureRegistry
    failure_manager.py     FailureManager — inject/recover failures
    failure_scenarios.py   Reusable failure scenario orchestrators
    exceptions.py          Failure framework exceptions
  state/                NetworkState, NodeState, LinkState
  events/               Event, EventBus, EventType (in-process)
  logging.py            Structured JSON logging
  launcher.py           CLI: up / down / reset / status (via NetworkController)
config/enterprise.yaml  Example enterprise topology
```

## Device classes

| Class  | Underlying | Role |
|---|---|---|
| `Router` | OVS switch | Multi-subnet gateway; interfaces declared in config. Routing (L3 forwarding between subnets) is a later module. |
| `Switch` | OVS switch | L2 access/distribution switching |
| `Host`   | Mininet host | End-user namespace with static IP + default route |
| `Server` | Mininet host | Server namespace; `services` metadata (http, postgres, ...) |

All switches are OpenFlow 1.3 datapaths bound to the Ryu controller, which
runs `topology_controller.py` (registry) + `l2_forwarding.py` (L2 learning
switch, enabling same-subnet connectivity).

## Topology config schema

```yaml
name: enterprise-demo
segments:
  - id: hq
    subnets:
      - { id: subnet-a, cidr: 10.0.1.0/24, gateway: 10.0.1.1 }
devices:
  routers:
    - name: rtr-core
      datapath_id: "0000000000000001"
      interfaces:
        - { port: 1, name: eth1, subnet: subnet-a, ip: 10.0.1.1/24 }
  switches:
    - { name: sw-a, datapath_id: "0000000000000011" }
  hosts:
    - { name: h1, subnet: subnet-a, ip: 10.0.1.10/24, gateway: 10.0.1.1 }
  servers:
    - { name: srv-web-1, subnet: subnet-a, ip: 10.0.1.20/24, gateway: 10.0.1.1, services: [http] }
links:
  - { src: rtr-core, dst: sw-a, src_port: 1, dst_port: 1 }
  - { src: sw-a, dst: h1 }
```

Validated at load time: unique names/dpids/subnet ids, IPs inside their
subnet, existing link endpoints, hosts with exactly one link, and every
subnet having hosts attached to a switch. Errors raise `ConfigError`.

## Network Control Layer (Submodule 1.3)

`NetworkController` is the **single interface** through which future modules
manipulate the virtual network. No future module calls Mininet APIs directly.

### Public API

| Method | Effect | Event published |
|---|---|---|
| `start_network()` | build + start Mininet, bind to Ryu | `NETWORK_STARTED` |
| `stop_network()` | stop Mininet, free resources | `NETWORK_STOPPED` |
| `restart_network()` | stop (if running) then start | `NETWORK_STOPPED` + `NETWORK_STARTED` |
| `reset_network()` | clean all artifacts, rebuild, start | `NETWORK_STARTED` + `NETWORK_RESET` |
| `enable_node(name)` | start a node | `NODE_ENABLED` |
| `disable_node(name)` | stop a node | `NODE_DISABLED` |
| `connect_link(a, b)` | bring link up | `LINK_CONNECTED` |
| `disconnect_link(a, b)` | bring link down | `LINK_DISCONNECTED` |
| `network_status()` | state summary dict | — |
| `topology_snapshot()` | topology + live state dict | — |
| `set_link_latency(a, b, ms)` | set added link latency (ms) | — |
| `clear_link_latency(a, b)` | clear link latency | — |
| `set_link_packet_loss(a, b, %)` | set link packet-loss % | — |
| `clear_link_packet_loss(a, b)` | clear link packet loss | — |
| `set_link_bandwidth(a, b, Mbps)` | set link bandwidth (Mbps) | — |
| `clear_link_bandwidth(a, b)` | clear link bandwidth | — |
| `impairments()` | active link impairments (canonical key → settings) | — |

### Network Impairments (Submodule 1.4C.1)

The controller exposes traffic-control APIs so later modules (e.g. the
`FailureManager`) can impose latency, packet-loss and bandwidth
impairments through the single Mininet-facing layer. No failure objects,
`FailureRegistry`, events, or `EventBus` are used here — validation and
state tracking happen inside the controller, and the change is applied to
the data plane via `configLinkImpairment(key, params)` (best-effort on a
stock Mininet link; recorded on a TCLink-backed or mocked net).

| Input | Valid range | Rejected value error |
|---|---|---|
| `latency_ms` | `>= 0` | `ValueError` |
| `loss_percent` | `0–100` | `ValueError` |
| `bandwidth_mbps` | `> 0` | `ValueError` |

Unknown links / unknown nodes raise `LinkNotFoundError`; operations on a
stopped network raise `NetworkNotRunningError`. Links are matched
canonically (order-insensitive).

### NetworkState

Mirrors the real network and is synchronized on every action:

- network: `running` / `stopped`
- nodes (`NodeState`): `online` / `offline`
- links (`LinkState`): `connected` / `disconnected` (canonical, order-insensitive)

### EventBus

In-process, thread-safe pub/sub. Every controller action publishes exactly
one `Event` (`EventType`, timestamp, source, data, result). No external
messaging.

```python
from network import NetworkController

ctrl = NetworkController()                     # reads config/enterprise.yaml
ctrl.event_bus.subscribe(lambda e: print(e.to_dict()))

ctrl.start_network()          # -> {"type": "NETWORK_STARTED", ...}
ctrl.disable_node("h1")       # -> {"type": "NODE_DISABLED", "data": {"node": "h1"}, ...}
ctrl.disconnect_link("sw-a", "h1")  # -> {"type": "LINK_DISCONNECTED", ...}
ctrl.network_status()         # -> {"status": "running", "nodes": [...], "links": [...]}
```

### Safety

Invalid operations raise meaningful exceptions (all subclass
`NetworkControlError`):

| Operation | Exception |
|---|---|
| start/reset while running | `NetworkAlreadyRunningError` |
| stop while stopped | `NetworkNotRunningError` |
| node/link ops while stopped | `NetworkNotRunningError` |
| enable online / disable offline node | `NodeAlreadyOnlineError` / `NodeAlreadyOfflineError` |
| connect connected / disconnect disconnected link | `LinkAlreadyConnectedError` / `LinkAlreadyDisconnectedError` |
| unknown node / link | `NodeNotFoundError` / `LinkNotFoundError` |

### Logging

Structured JSON lines with timestamp, level, message, event type and result:

```json
{"timestamp": "...", "level": "INFO", "logger": "network.control",
 "message": "Node h1 Disabled", "event": "NODE_DISABLED", "result": "success"}
```

### Run (Linux host, `sudo modprobe openvswitch` first)

```bash
make net-up        # build stack + start enterprise network
make net-status    # show OVS bridges/ports
make net-down      # stop + clean all Mininet artifacts
make net-reset     # clean, rebuild, start
```

Or directly: `docker compose exec -T mininet-net python3 -m network.launcher up`

## Failure Models & Registry (Submodule 1.4A)

The foundation of the Failure Injection Framework: a **data model**,
an **in-memory registry** and **exceptions** only. Nothing here touches
the `NetworkController`, the `EventBus`, or Mininet. The `FailureManager`
(Submodule 1.4B.1) builds on top of this submodule.

### Failure types

Strongly typed dataclasses in `failure_types.py`, all subclassing
`Failure` (id, timestamp, severity, affected_nodes, state,
recovered_at, injected_by, metadata):

| Class | Type-specific fields | `FailureType` |
|---|---|---|
| `RouterFailure` | `router` | `ROUTER` |
| `SwitchFailure` | `switch` | `SWITCH` |
| `LinkFailure` | `src`, `dst` (+ canonical `link` name) | `LINK` |
| `LatencyFailure` | `node`, `latency_ms` | `LATENCY` |
| `PacketLossFailure` | `node`, `loss_percent` | `PACKET_LOSS` |
| `BandwidthFailure` | `node`, `bandwidth_bps` | `BANDWIDTH` |

Enums: `FailureState` (`ACTIVE` / `RECOVERED`), `Severity` (`LOW` /
`MEDIUM` / `HIGH` / `CRITICAL`), `FailureType` (six kinds above).
`affected_nodes` is auto-populated from the type-specific fields unless
given explicitly. Every failure has a unique auto-generated `id`, a UTC
`timestamp`, and a serializable `to_dict()`.

### FailureRegistry

In-memory store (in `failure_registry.py`) tracking active failures,
recovered failures, failure history, recovery timestamps and affected
devices:

| Method | Behaviour |
|---|---|
| `register(failure)` | track a failure; raises `DuplicateFailureError` on duplicate id |
| `mark_recovered(id, recovered_at=None)` | set `RECOVERED` + stamp recovery time (idempotent); raises `FailureNotFoundError` if unknown |
| `active_failures()` | list of `ACTIVE` failures |
| `recovered_failures()` | list of `RECOVERED` failures |
| `history()` | every tracked failure, in registration order |
| `get(id)` | failure or `None` |
| `affected_devices()` | set of all device names ever affected |
| `clear()` | remove all tracked failures |

Optional `valid_nodes` / `valid_links` sets enable topology validation at
construction (raises `UnknownNodeError` / `UnknownLinkError`).

### Exceptions

All derive from `FailureError`: `DuplicateFailureError`,
`FailureNotFoundError`, `UnknownNodeError`, `UnknownLinkError`,
`InvalidLatencyError`, `InvalidPacketLossError`, `InvalidBandwidthError`.

```python
from network import FailureRegistry, RouterFailure

registry = FailureRegistry()
failure = registry.register(RouterFailure(router="rtr-core"))
registry.mark_recovered(failure.id)
registry.history()          # -> [RouterFailure(state=RECOVERED, ...)]
registry.affected_devices() # -> {"rtr-core"}
```

## FailureManager (Submodule 1.4B.1)

`FailureManager` is the **only interface for injecting and recovering
failures**. It never touches Mininet — every data-plane operation goes
through the `NetworkController` (which remains the only interface to
Mininet). It reuses the `FailureRegistry` for state tracking, the failure
dataclasses and exception hierarchy, and the controller's `EventBus` for
event publication.

Router and switch failure injection/recovery (Submodule 1.4B.1) plus link
failure support (Submodule 1.4B.2) and network impairment integration
(Submodule 1.4C.2) are complete. The `NetworkController` exposes the
underlying traffic-control APIs (Submodule 1.4C.1); the `FailureManager`
consumes them for latency, packet-loss and bandwidth failures.

### Public API

| Method | Effect | Event published |
|---|---|---|
| `inject_router_failure(node)` | take a router down + record failure | `NODE_DISABLED` (controller) + `FAILURE_INJECTED` |
| `recover_router(node)` | bring a failed router back up | `NODE_ENABLED` (controller) + `FAILURE_RECOVERED` |
| `inject_switch_failure(node)` | take a switch down + record failure | `NODE_DISABLED` (controller) + `FAILURE_INJECTED` |
| `recover_switch(node)` | bring a failed switch back up | `NODE_ENABLED` (controller) + `FAILURE_RECOVERED` |
| `disconnect_link(a, b)` | take a link down + record failure | `LINK_DISCONNECTED` (controller) |
| `reconnect_link(a, b)` | bring a failed link back up | `LINK_CONNECTED` (controller) |
| `inject_latency(a, b, ms)` | impose latency on a link | `LATENCY_INJECTED` |
| `remove_latency(a, b)` | remove latency impairment | `FAILURE_RECOVERED` |
| `inject_packet_loss(a, b, %)` | impose packet loss on a link | `PACKET_LOSS_INJECTED` |
| `remove_packet_loss(a, b)` | remove packet-loss impairment | `FAILURE_RECOVERED` |
| `limit_bandwidth(a, b, Mbps)` | cap bandwidth on a link | `BANDWIDTH_LIMITED` |
| `remove_bandwidth_limit(a, b)` | remove bandwidth cap | `FAILURE_RECOVERED` |
| `active_failures()` | list of active failures | — |
| `reset_all_failures()` | recover every active failure | per-failure events + `NETWORK_RESTORED` |

`FAILURE_INJECTED` / `FAILURE_RECOVERED` / `NETWORK_RESTORED` /
`LATENCY_INJECTED` / `PACKET_LOSS_INJECTED` / `BANDWIDTH_LIMITED`
were added to `EventType`; the original seven members are unchanged
(backward compatible). Link operations keep using the existing
`LINK_DISCONNECTED` / `LINK_CONNECTED` events.

### Validation

- **Unknown node** → `UnknownNodeError` (checked against the topology).
- **Unknown link** (or a self-link) → `UnknownLinkError`.
- **Wrong device kind** (e.g. `inject_router_failure("h1")`) →
  `FailureError`.
- **Duplicate failure** (failure already active on the node or link) →
  `DuplicateFailureError`.
- **Recovering a healthy device/link** (no active failure) →
  `FailureNotFoundError`.

Every injection and recovery is also logged as a structured JSON line via
`network.logging` (logger `failure.manager`, `event` + `result` fields).

```python
from network import FailureManager, NetworkController

controller = NetworkController()
controller.start_network()
manager = FailureManager(controller)

failure = manager.inject_router_failure("rtr-core")
manager.active_failures()   # -> [RouterFailure(state=ACTIVE, ...)]
manager.recover_router("rtr-core")
manager.active_failures()   # -> []

manager.disconnect_link("sw-a", "h1")
manager.active_failures()   # -> [LinkFailure(state=ACTIVE, ...)]

manager.inject_latency("sw-a", "h1", 50)
manager.active_failures()   # -> [LatencyFailure(state=ACTIVE, ...)]
manager.remove_latency("sw-a", "h1")

manager.limit_bandwidth("sw-a", "h1", 100)
manager.remove_bandwidth_limit("sw-a", "h1")

manager.reset_all_failures()  # recover everything -> NETWORK_RESTORED
```

## Failure Scenarios (Submodule 1.4D)

Reusable orchestrators that wrap the `FailureManager` for common failure
patterns. Each scenario is a thin state-machine (IDLE → RESTORED) that
injects a specific failure on execute and recovers it on restore.

| Scenario | Failure type | Default target |
|---|---|---|
| `ScenarioRouterFailure` | Router down | `rtr-core` |
| `ScenarioSwitchFailure` | Switch down | `sw-a` |
| `ScenarioCableCut` | Link disconnected | `sw-a` ↔ `h1` |
| `ScenarioHighLatency` | Latency 100 ms | `sw-a` ↔ `h1` |
| `ScenarioPacketLoss` | Packet loss 10 % | `sw-a` ↔ `h1` |
| `ScenarioBandwidthConstraint` | Bandwidth 10 Mbps | `sw-a` ↔ `h1` |

### Public API

| Method | Effect |
|---|---|
| `execute()` | inject failure, return summary dict |
| `restore()` | recover failure, return summary dict |
| `status()` | current state + failure id |
| `summary()` | full execution summary |

Scenarios are single-use: after one execute→restore cycle the scenario
is completed and cannot be re-executed.

```python
from network import FailureManager, ScenarioRouterFailure, NetworkController

controller = NetworkController()
controller.start_network()
manager = FailureManager(controller)

scenario = ScenarioRouterFailure(manager)
result = scenario.execute()   # -> {"class": "ScenarioRouterFailure", "state": "RESTORED", ...}
scenario.restore()            # -> {"state": "IDLE", "failure": None, ...}
```

## Test

```bash
make net-test      # all unit + integration tests in-container (Modules 1.1-1.4)
```

- Unit: bootstrap (1.1), topology/config (1.2), NetworkController + state +
  events (1.3 — mocked Mininet, runs anywhere), failure models + registry
  (1.4A — stdlib only), FailureManager router/switch/link/impairment core
  (1.4B + 1.4C — mocked Mininet, runs anywhere), failure scenarios
  (1.4D — mocked Mininet, runs anywhere).
- Integration: build the real network, wait for OVS→Ryu handshake, verify
  same-subnet ping between every pair of hosts/servers plus the presence of
  installed learning flows.

## Extending

- Add devices: edit `config/enterprise.yaml` (or a new YAML, pass
  `--config`).
- New device kinds: add a class in `devices.py` and map it in
  `DEVICE_CLASSES` (`enterprise_topology.py`).
- New validation rules: extend `config._validate()`.

## Telemetry & Monitoring (Module 2 — simulated source)

`network.telemetry` collects, stores and evaluates network telemetry. Its
modules import only the standard library and the events layer, never
Mininet. Note that importing it as `network.telemetry` still runs
`network/__init__.py`, which needs the `mininet` Python package; the
viewer's offline loader (`viewer/_offline.py`) loads the telemetry modules
individually to avoid that.

> **All telemetry currently comes from `SimulatedSource`.** It is generated
> from `NetworkState` and the controller's impairment settings, so it reacts
> to every failure injected through `FailureManager`, but it is **not** a
> measurement of the real data plane. Every sample carries
> `simulated=True` and its source name. Live sources (Ryu OpenFlow port
> statistics, ping/iperf probes) plug into the same interface once the Linux
> lab exists.

```
TelemetrySource(s) -> Sample[] -> RateDeriver -> TelemetryStore
                                              -> HealthEvaluator -> HEALTH_CHANGED
```

| Component | File | Role |
|---|---|---|
| `Metric`, `Sample`, `METRIC_SPECS` | `telemetry/metrics.py` | data model: 13 metrics (gauges, counters, derived) |
| `TelemetryStore` | `telemetry/store.py` | thread-safe ring buffer per (metric, target) series |
| `RateDeriver` | `telemetry/derive.py` | counters -> throughput / utilization / drop rate; handles counter resets |
| `TelemetrySource`, `SimulatedSource` | `telemetry/sources/` | source interface + deterministic simulation |
| `HealthEvaluator`, `Thresholds` | `telemetry/health.py` | HEALTHY / DEGRADED / DOWN / UNKNOWN with reasons |
| `TelemetryCollector` | `telemetry/collector.py` | runs the pipeline once or on a background thread |

### Metrics

| Metric | Kind | Unit |
|---|---|---|
| `link.status`, `node.status` | gauge | 1 up / 0 down |
| `link.latency_ms` | gauge | one-way ms |
| `link.packet_loss_pct` | gauge | % (probe-measured) |
| `link.capacity_mbps` | gauge | Mbps (bandwidth limit or line rate) |
| `link.bytes`, `link.packets`, `link.dropped_packets` | counter | cumulative |
| `link.throughput_mbps`, `link.utilization_pct`, `link.drop_rate_pps` | derived | from counter deltas |
| `node.cpu_pct`, `node.memory_pct` | gauge | % |

### Simulation model

- A link is up when the network runs, the link is connected and both
  endpoints are online. Down links report 100 % loss and carry no traffic.
- Latency = 0.05 ms base + configured impairment + jitter.
- Loss is measured by 50 simulated probes per sample, so a 10 % setting
  reads around 10 % with realistic noise.
- Offered load follows a periodic pattern (uplinks 120, server links 60,
  access links 20 Mbps); delivered traffic is capped by capacity and
  reduced by loss; the rest counts as dropped.
- Counters reset when a switch/router restarts, like real port counters.
- Same seed + same clock = identical output (reproducible experiments).

### Health thresholds (defaults)

latency > 50 ms, loss > 2 %, utilization > 85 %, drop rate > 100 pps,
CPU > 90 %, memory > 90 % -> `DEGRADED`; status 0 -> `DOWN`.

### Events

| Event | When |
|---|---|
| `TELEMETRY_COLLECTED` | every collection (summary only; `result="partial"` if a source failed) |
| `TELEMETRY_SOURCE_ERROR` | a source raised; other sources still run |
| `HEALTH_CHANGED` | a target's status changed (transitions only) |

```python
from network.telemetry import SimulatedSource, TelemetryCollector

collector = TelemetryCollector(
    [SimulatedSource(controller, seed=1)],
    event_bus=controller.event_bus,
    interval_s=5,
)
collector.collect_once()          # or collector.start() / collector.stop()
collector.health()["link:h1-sw-a"].status
collector.store.series("link.latency_ms", "h1-sw-a")
```

### Viewer API

`/api/telemetry/status`, `/api/telemetry/metrics`, `/api/telemetry/health`,
`/api/telemetry/latest?metric=&target=`,
`/api/telemetry/series?metric=&target=&limit=`. In offline preview the
network is stopped (everything reads DOWN); start the viewer with
`VIEWER_SIMULATE_NETWORK=1` to simulate a running network.
`VIEWER_TELEMETRY_INTERVAL` sets the collection interval (default 5 s).
