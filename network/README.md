# network/ — Virtual Enterprise Network (Module 1)

Containerized Mininet + Open vSwitch data plane with a **configurable
enterprise topology generator** (Submodule 1.2) and a **Network Control
Layer** (Submodule 1.3).

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

## Test

```bash
make net-test      # all unit + integration tests in-container (Modules 1.1-1.3)
```

- Unit: bootstrap (1.1), topology/config (1.2), NetworkController + state +
  events (1.3 — mocked Mininet, runs anywhere).
- Integration: build the real network, wait for OVS→Ryu handshake, verify
  same-subnet ping between every pair of hosts/servers plus the presence of
  installed learning flows.

## Extending

- Add devices: edit `config/enterprise.yaml` (or a new YAML, pass
  `--config`).
- New device kinds: add a class in `devices.py` and map it in
  `DEVICE_CLASSES` (`enterprise_topology.py`).
- New validation rules: extend `config._validate()`.
