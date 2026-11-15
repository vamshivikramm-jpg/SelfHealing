# Deployment and setup

The project runs in two very different settings. Be clear which one you
are in: everything verified so far ran in **setting A**.

| | A. Simulation (any Mac/Linux) | B. Live lab (Linux VM) |
|---|---|---|
| Network | in-memory simulated data plane | Mininet + Open vSwitch + Ryu in Docker |
| Telemetry | `SimulatedSource` (reads the simulated network's real state) | live sources: **not built yet** |
| Self-healing execution | `SimulatedExecutor` | live executor: **not built yet** |
| Status | Modules 1–9 tested (unit tests, evaluations, dashboard) | Module 1 only, and never run on this project's hardware |

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

## B. Live lab (Linux VM) — planned, not yet verified

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

Known risk: `pip install ryu==4.34` can fail on new setuptools
(`easy_install` / `get_script_args`); pin setuptools in
`controller/Dockerfile` if it does.

### Before the live network can run Modules 2–8 (Module 2.0 and beyond)

1. **Fix re-enabling nodes**: `NetworkController.enable_node` calls
   `start()` without the controllers argument (switches) and hosts have no
   `start()`; switches must be stopped with `deleteIntfs=False`.
2. **Apply impairments for real**: build links with `TCLink` and configure
   `intf.config(delay=, loss=, bw=)` (the current `configLinkImpairment`
   call does not exist in Mininet).
3. **Scenario state names** are reversed (`RESTORED` after execute).
4. **Live telemetry sources**: Ryu OpenFlow port/flow statistics and
   ping/iperf probes behind the existing `TelemetrySource` interface.
5. **Live executor**: NetworkController calls + Ryu `ofctl_rest` for
   routes and Zero Trust rules (rendering exists, untested).
6. **MAC addresses** from `config/identities.yaml` must be assigned in Mininet.
7. **Redundant topologies** (`enterprise-redundant.yaml`, generated campus)
   need loop-aware forwarding in Ryu before they run on Mininet.
8. **Inter-subnet routing** (rtr-core is an L2 switch today).

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
