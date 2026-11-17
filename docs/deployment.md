# Deployment and setup

The project runs in two very different settings. Be clear which one you
are in: everything verified so far ran in **setting A**.

| | A. Simulation (any Mac/Linux) | B. Live lab (Linux VM) |
|---|---|---|
| Network | in-memory simulated data plane | Mininet + Open vSwitch + Ryu in Docker |
| Telemetry | `SimulatedSource` (reads the simulated network's real state) | `LiveSource` (active probes + OpenFlow counters) |
| Self-healing execution | `SimulatedExecutor` | `LiveExecutor` (Ryu REST: pinned paths, drop rules) |
| Status | Modules 1–9 tested (unit tests, evaluations, dashboard) | **Module 1, failure control, Ryu app, live telemetry and healing actions verified live** (32 integration tests); live closed loop not yet; Modules 2–8 not yet |

## A. Simulation setup

Requirements: Python 3.9+ and the packages in `requirements-dev.txt`
(PyYAML, pytest, numpy, scikit-learn, joblib, scipy, FastAPI, uvicorn, and
the `mininet` *Python package*, which is imported but never started).

```bash
git clone https://github.com/vamshivikramm-jpg/SelfHealing.git
cd SelfHealing
python3 -m pip install -r requirements-dev.txt
```

On the development Mac the system `python3` already has these packages.
The project `.venv` only has FastAPI/uvicorn/PyYAML: it runs the read-only
offline preview, not the simulation mode.

| Task | Command |
|---|---|
| Unit tests | `PYTHONPATH=network/src python3 -m pytest tests/unit` |
| Dashboard (full closed loop, operator controls) | `VIEWER_MODE=simulation python3 -m uvicorn viewer.app:app --host 127.0.0.1 --port 8000` |
| Read-only offline preview | `python3 -m uvicorn viewer.app:app --host 127.0.0.1 --port 8000` |
| Generate a labelled dataset (Module 3) | `PYTHONPATH=network/src python3 -m network.analytics.generate --out data/run1 --duration 7200 --failures 8` |
| Compare prediction models (Module 4) | `PYTHONPATH=network/src python3 -m network.prediction.experiment --out data/module4` |
| Zero Trust evaluation (Module 5) | `PYTHONPATH=network/src python3 -m network.security.evaluation --out data/module5` |
| Decision evaluation (Module 6) | `PYTHONPATH=network/src python3 -m network.decision.evaluation --out data/module6` |
| Healing evaluation (Module 7) | `PYTHONPATH=network/src python3 -m network.healing.evaluation --out data/module7` |
| All experiments, quick (~4 min) | `PYTHONPATH=network/src python3 -m network.experiments.runner --profile quick --out results/quick` |
| All experiments, full (~70 min) | `PYTHONPATH=network/src python3 -m network.experiments.runner --profile full --out results/full` |

`data/`, `results/` and `*.db` are git-ignored.

### Viewer environment variables

| Variable | Effect |
|---|---|
| `VIEWER_MODE=simulation` | run the Modules 2–7 closed loop and enable the Control tab |
| `VIEWER_SIM_INTERVAL` | simulation step in seconds (default 2) |
| `VIEWER_SIM_PREDICTOR=0` | skip training the start-up predictor (faster start) |
| `VIEWER_SIMULATE_NETWORK=1` | offline preview: pretend the topology is running |
| `VIEWER_TELEMETRY_INTERVAL` | offline preview: collection interval (default 5 s) |
| `VIEWER_DB_PATH=file.db` | offline preview: record telemetry history (Analytics tab) |
| `VIEWER_SIMULATE_SECURITY=1`, `VIEWER_SECURITY_ATTACKS=1` | offline preview: Zero Trust on simulated traffic |
| `VIEWER_DECISIONS=1` | offline preview: run the decision engine |
| `VIEWER_MODEL_DIR=dir` | use a trained model registry (needs numpy/scikit-learn) |

Security: the viewer has **no login**. Operator controls exist only in
simulation mode and answer 403 otherwise. Always bind to `127.0.0.1`.

## B. Live lab (Linux VM) — Module 1 verified

First verified on 17 Nov 2026: Ubuntu 26.04 VM (2 vCPU, 3 GB RAM, 10 GB root
volume), Docker 29, Compose 2.40. Both containers healthy; switches connect to
Ryu; all same-subnet hosts and servers reach each other; learning flows are
installed on the switches (`make net-test`: 6 integration + 215 unit tests).

Docker on macOS has no Open vSwitch kernel module, so the live network
needs a Linux VM. On the Apple-silicon Mac, use UTM with Ubuntu Server
24.04 ARM64 (4 vCPU, 6–8 GB RAM, 40 GB disk, OpenSSH enabled).

```bash
sudo apt update && sudo apt -y upgrade
sudo apt install -y docker.io docker-compose-v2 git make python3-pytest
sudo usermod -aG docker $USER            # then log out and back in
echo openvswitch | sudo tee /etc/modules-load.d/openvswitch.conf && sudo modprobe openvswitch
git clone https://github.com/vamshivikramm-jpg/SelfHealing.git && cd SelfHealing && cp .env.example .env
docker compose up -d --build             # mininet-net + ryu-controller
docker compose ps                        # both must be "healthy"
make smoke && make net-test              # first live baseline
```

Lessons from the first live build (already fixed in the repository):

- **Ryu 4.34** needs an old toolchain: `controller/Dockerfile` now uses
  Python 3.9, setuptools 57.5 with `--no-build-isolation`, and eventlet 0.30.2.
- **Ubuntu 24.04 pip (PEP 668)**: `network/Dockerfile` installs pytest from apt
  and the project with `--break-system-packages`.
- **DNS inside Docker builds** can fail on some VMs ("server misbehaving",
  "Temporary failure in name resolution") even when the VM itself resolves
  names. Build with the host network, then start without rebuilding:

  ```bash
  docker build --network host -t selfhealing-ryu-controller ./controller
  docker build --network host -t selfhealing-mininet-net ./network
  docker compose up -d
  ```
- **Disk**: Ubuntu's installer gives the root volume only part of the disk
  (10 GB here); the two images need about 1.3 GB. Extend it with
  `sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv` if space runs low.
- **Pasting commands**: the VM console has no clipboard; connect from the Mac
  with `ssh user@<vm-ip>` instead (`ip -4 addr show` shows the address).

### Live failure control (P1) — verified

`tests/integration/test_failures_live.py` (7 tests, run by `make net-test`)
checks on the real network: identity MACs are assigned; 50 ms latency per
direction raises ping RTT to ~100 ms and clears; 100 % loss stops traffic;
a 5 Mbit bandwidth limit installs an htb rate; a link, a switch (OVS bridge
restarted with its controller) and a host go down and come back.
`make net-test` runs the checked-out code (`PYTHONPATH=/workspace/network/src`),
not the copy baked into the image.

### Self-healing Ryu app (P2) — verified

`controller/apps/selfhealing_app.py` replaces the L2 learning switch:
proxy ARP, virtual gateways, loop-safe routing within and between subnets,
blocks, port/flow statistics and a REST API (`controller/README.md`).
`tests/integration/test_routing_live.py` (12 tests) checks inter-subnet
routing for all 42 host pairs, gateway ping, blocks, stats, and on the
redundant topology: no broadcast storm, failover when sw-a↔sw-b goes down,
and traffic following a pinned path. After changing the app, rebuild its
image: `docker build --network host -t selfhealing-ryu-controller ./controller && docker compose up -d`.

### Live telemetry and healing back-ends (P3–P4) — verified

`network/src/network/live/` implements the simulation's interfaces on the
real network:

| Piece | How it measures / acts |
|---|---|
| `LiveSource` | per-link one-way latency and loss from the Ryu app's active probes (probe frames across switch links, ARP probes to hosts, 5 per second, 5 s window, controller↔switch delay subtracted using OpenFlow echo); carrier from port state; byte/packet/drop counters from OpenFlow port stats. A link that drops every probe is **down** even if the controller thinks it is connected. No per-node CPU/memory (Mininet nodes share one kernel). |
| `LiveFaults` | "physical" faults: `tc netem loss 100%` on a link or all links of a switch, without telling the controller |
| `LiveServiceModel` | delivery per client/server pair from real pings (3 per step); routes read back from Ryu |
| `LiveExecutor` | reroute = pin both directions in Ryu; quarantine/block = drop rules for MAC and IP; undo restores pins / removes rules; restore uses NetworkController/FailureManager |
| `LiveVerifier` | `access_blocked` = a real ping from the device gets no answer |

`tests/integration/test_live_backends.py` (7 tests) checks on the redundant
topology: clean measurements on a healthy network; 50 ms impairment measured
as 40–65 ms; 30 % loss measured as 10–55 %; a physical cut seen only by
measurement; disconnected link and failed switch; a reroute pinned through
Ryu restoring delivery around a cut link (verified with pings); a quarantine
dropping exactly one device's traffic.

### Still needed for the live dashboard

1. **Real traffic and attacks** in the hosts, Zero Trust from real flows (P5).
2. **Live runtime** wiring these back-ends into the closed loop, served by
   the dashboard from the VM (P6).

## Continuous integration

`.github/workflows/tests.yml` runs the unit tests on Python 3.9 and 3.11
for every push to `main` and every pull request, then a quick experiment
smoke run whose report is uploaded as an artifact. CI installs
`requirements-dev.txt`; Mininet comes from its GitHub 2.3.0 release
because it is not on PyPI.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard shows "OFFLINE PREVIEW" although `VIEWER_MODE=simulation` | the Python environment lacks `mininet`/numpy/scikit-learn; `/api/health` → `simulation_error` says which; use the system `python3` or install `requirements-dev.txt` |
| Topology panel says it needs internet | Cytoscape loads from unpkg on first load |
| `/api/predictions` answers 503 | numpy/scikit-learn missing, or no model registry |
| Flood/volume attacks not detected right after start | detectors learn normal traffic for ~20 steps first |
| `ValueError: N failures need about … s` | too many failures for the duration in `generate_plan`; lengthen `--duration` |
| Everything reads DOWN in the offline preview | correct: no network is running; use `VIEWER_SIMULATE_NETWORK=1` or simulation mode |
