# controller/ — Ryu SDN controller

OpenFlow 1.3 controller container: Python 3.9, `ryu==4.34`, `eventlet==0.30.2`,
`PyYAML` (see the Dockerfile for why these versions).

## App: `apps/selfhealing_app.py`

`ryu-manager --wsapi-port 8080 apps/selfhealing_app.py` runs one app. The
forwarding decisions live in `apps/sh_logic.py` (pure Python, unit tested by
`tests/unit/test_ryu_logic.py`); `selfhealing_app.py` only speaks OpenFlow and
REST.

The controller reads the same files Mininet is built from
(`network/config`, mounted read-only at `/opt/controller/config`): the
topology (`SH_TOPOLOGY`, default `enterprise.yaml`) and `identities.yaml`.

| Feature | How |
|---|---|
| Proxy ARP | all ARP goes to the controller; host IP → registered MAC, gateway IP → virtual gateway MAC `02:fe:<ip>`. Nothing is flooded between switches, so redundant links (loops) are safe. |
| Routing | per destination host, a BFS shortest-path tree over live links; one rule per switch on table 1. At the last switch, packets not addressed to the host's MAC are routed: MACs rewritten, TTL decremented. |
| Gateway ping | ICMP echo to a gateway IP is answered by the controller. |
| Host discovery | learned from packets on edge ports; unknown destinations are found with an ARP probe from their gateway, on edge ports only. |
| Failover | port down/up or a switch disconnecting recomputes every path at once. |
| Pinned paths | `PUT /sh/routes/<ip>` forces a switch path for a destination (used by self-healing reroutes); ignored while one of its links is down. |
| Blocks | table 0 drop rules (priority 1000) on every switch, e.g. for Zero Trust. |
| Statistics | port and flow counters polled every `SH_STATS_INTERVAL` s (default 2). |

### REST API (port 8080, inside the compose network: `http://ryu-controller:8080`)

| Method | Path | Body / result |
|---|---|---|
| GET | `/sh/health` | status, topology, connected switches |
| GET | `/sh/topology` | switches, links (live?), located hosts |
| PUT | `/sh/config` | `{"topology": "enterprise-redundant.yaml"}` — route for another topology file |
| GET | `/sh/routes` | per destination IP: path from every switch, pin |
| PUT | `/sh/routes/<ip>` | `{"path": ["sw-a", "rtr-core", "sw-b"]}` — pin |
| DELETE | `/sh/routes/<ip>` | unpin |
| GET / POST | `/sh/blocks` | `{"match": {"ipv4_src": "10.0.2.10", "tcp_dst": 22}, "reason": "..."}`; fields: `eth_src eth_dst ipv4_src ipv4_dst ip_proto tcp_dst udp_dst` |
| DELETE | `/sh/blocks/<id>` | remove a block |
| GET | `/sh/stats/ports`, `/sh/stats/flows` | latest counters |

The API has no authentication: it is only reachable on the internal compose
network. Do not publish port 8080.

## Run

```bash
docker build --network host -t selfhealing-ryu-controller ./controller   # after changing the app
docker compose up -d
docker compose logs -f ryu-controller
```

Ryu listens on TCP 6653 (OpenFlow) and 8080 (REST). The container
healthcheck probes 6653. Verified live by `tests/integration/test_routing_live.py`
(`make net-test`).
