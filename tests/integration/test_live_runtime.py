"""The whole closed loop on the live network (live P6).

Requires a running stack (mininet-net + ryu-controller) and NO running
live dashboard (both would drive the same Mininet):
    make live-dashboard-stop; make net-test

Builds the dashboard's LiveRuntime (short warm-up), checks that failures
are refused while the predictor learns, then cuts sw-a <-> sw-b physically
and expects a verified reroute, and launches a real port scan and expects
the attacker to be quarantined and the quarantine verified by ping.
"""

import pathlib
import subprocess
import time

import pytest

CFG = pathlib.Path(__file__).resolve().parents[2] / "network" / "config"

if subprocess.run(["pgrep", "-f", "^python3 -m uvicorn viewer.app"], capture_output=True).returncode == 0:
    pytest.skip("the live dashboard is running; stop it first (make live-dashboard-stop)",
                allow_module_level=True)


def _wait(runtime, predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime.tick()
        if predicate():
            return True
        time.sleep(max(0.0, runtime.interval_s - 0.5))
    return False


@pytest.fixture(scope="module")
def runtime():
    from network.live.runtime import LiveRuntime

    rt = LiveRuntime(str(CFG), interval_s=2.0, warmup_s=30.0)
    yield rt
    rt.shutdown()


def _actions(rt, kind, target):
    return [a for a in rt.healing.history() if a.get("type") == kind and a.get("target") == target]


def test_refuses_failures_while_learning(runtime):
    from network.simulation.runtime import RuntimeError_

    with pytest.raises(RuntimeError_, match="learning"):
        runtime.inject_failure("link", ["sw-a", "sw-b"], 30, physical=True)


def test_predictor_learns_from_live_warmup(runtime):
    assert _wait(runtime, lambda: runtime.predictor is not None, 150), runtime.trainer.status()
    assert 0.8 <= runtime.predictor.threshold <= 0.95
    assert runtime.status()["learning"] is False


def test_physical_cut_is_rerouted_and_verified(runtime):
    runtime.inject_failure("link", ["sw-a", "sw-b"], 60, physical=True)
    ok = _wait(runtime, lambda: any(a["state"] == "VERIFIED" for a in _actions(runtime, "REROUTE", "link:sw-a-sw-b")),
               60)
    assert ok, runtime.healing.history()[:3]
    assert runtime.rerouted(), "no demand moved off the cut link"


def test_real_port_scan_is_quarantined_and_verified(runtime):
    runtime.launch_attack("port_scan", "h5", 40)
    assert _wait(runtime, lambda: "h5" in runtime.security.quarantined(), 40), runtime.security.status()
    ok = _wait(runtime, lambda: any(a["state"] == "VERIFIED" for a in _actions(runtime, "QUARANTINE", "node:h5")),
               30)
    assert ok, runtime.healing.history()[:3]
