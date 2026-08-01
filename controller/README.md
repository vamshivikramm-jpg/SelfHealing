# controller/ — Ryu SDN Controller (Module 1)

OpenFlow 1.3 controller container, pinned to known-good versions:
`ryu==4.34` + `eventlet==0.33.3` (Python 3.11 compatible).

## Apps

Both apps run on startup (`ryu-manager apps/topology_controller.py apps/l2_forwarding.py`):

- `apps/topology_controller.py` — maintains a live switch/port registry and
  exposes `get_topology()` for later modules.
- `apps/l2_forwarding.py` — L2 learning switch (same-subnet connectivity for
  the Submodule 1.2 topology). MAC table flows expire via idle timeout.
  This is switching, not routing — inter-subnet forwarding arrives with a
  later module.

## Run

```bash
docker compose up -d
docker compose logs -f ryu-controller
```

Ryu listens on TCP 6653. The container healthcheck probes that port.
