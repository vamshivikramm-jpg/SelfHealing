"""Unit tests for Module 8.2 — dashboard API in simulation and read-only modes.

Calls the FastAPI handlers directly (no HTTP client needed). Skipped when
FastAPI is not installed in the test environment.
"""

import asyncio
import importlib
import sys

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402


def load_app(monkeypatch, **env):
    for key in ("VIEWER_MODE", "VIEWER_SIM_PREDICTOR", "VIEWER_SIM_INTERVAL", "VIEWER_DECISIONS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("viewer.app", None)
    return importlib.import_module("viewer.app")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sim(monkeypatch):
    app = load_app(monkeypatch, VIEWER_MODE="simulation", VIEWER_SIM_PREDICTOR="0", VIEWER_SIM_INTERVAL="2")
    app._ensure_singletons()
    clock = {"t": app._runtime.now}
    app._runtime._clock = lambda: clock["t"]

    def advance(seconds):
        for _ in range(int(seconds / 2)):
            clock["t"] += 2
            app._runtime.tick()

    app._advance = advance
    yield app
    sys.modules.pop("viewer.app", None)


def test_simulation_mode_overview(sim):
    sim._advance(6)
    health = run(sim.health())
    assert health["mode"] == "simulation" and health["controls_enabled"] and health["simulation_error"] is None
    o = run(sim.overview())
    assert o["mode"] == "simulation" and o["simulated"] and o["controls_enabled"]
    assert len(o["topology"]["nodes"]) == 10 and len(o["topology"]["links"]) == 10
    assert o["health_counts"] == {"HEALTHY": 20}
    assert o["decisions"]["active"] == [] and o["healing"]["stats"]["executed"] == 0
    assert o["security"]["trust"][0]["device"] == "h1"


def test_inject_heal_and_views(sim):
    sim._advance(6)
    item = run(sim.sim_failure(sim.FailureRequest(kind="link", target=["sw-a", "sw-b"], duration_s=60,
                                                    physical=True)))
    assert item["state"] == "scheduled"
    sim._advance(20)
    o = run(sim.overview())
    core = next(l for l in o["topology"]["links"] if l["name"] == "sw-a-sw-b")
    assert core["status"] == "DOWN"
    assert o["rerouted"] and any(l["rerouted"] for l in o["topology"]["links"])
    assert {a["type"] for a in o["healing"]["actions"]} >= {"RESTORE", "REROUTE"}
    series = run(sim.series(target="sw-a-sw-b", metrics="link.status,link.latency_ms", limit=50))
    assert series["series"]["link.status"][-1][1] == 0.0
    tl = run(sim.timeline(types="HEALTH_CHANGED", limit=10))
    assert tl["events"] and all(e["type"] == "HEALTH_CHANGED" for e in tl["events"])
    heal = run(sim.healing())
    assert heal["simulated"] and heal["stats"]["executed"] >= 2
    assert run(sim.sim_injections())["injections"][0]["state"] == "active"
    summary = run(sim.analytics_summary(window_s=300))
    assert summary["summary"]["simulated"] == "simulated"


def test_controls_validation_and_approval(sim):
    with pytest.raises(HTTPException) as bad:
        run(sim.sim_failure(sim.FailureRequest(kind="meteor", target=["x"])))
    assert bad.value.status_code == 400
    with pytest.raises(HTTPException) as bad_level:
        run(sim.sim_autonomy(sim.AutonomyRequest(level="L9")))
    assert bad_level.value.status_code == 400
    assert run(sim.sim_autonomy(sim.AutonomyRequest(level="L1"))) == {"autonomy": "L1"}
    run(sim.sim_failure(sim.FailureRequest(kind="link", target=["sw-a", "h1"], duration_s=40)))
    sim._advance(6)
    pending = run(sim.overview())["decisions"]["pending"]
    assert pending and pending[0]["type"] == "RESTORE"
    assert run(sim.approve_intent(pending[0]["id"]))["status"] == "APPROVED"
    with pytest.raises(HTTPException) as again:
        run(sim.approve_intent(pending[0]["id"]))
    assert again.value.status_code == 409
    with pytest.raises(HTTPException) as missing:
        run(sim.reject_intent("intent-nope"))
    assert missing.value.status_code == 409
    attack = run(sim.sim_attack(sim.AttackRequest(kind="port_scan", attacker="h4", duration_s=20)))
    assert attack["type"] == "attack"
    assert len(run(sim.sim_demo())["scheduled"]) == 5


def test_read_only_outside_simulation(monkeypatch):
    app = load_app(monkeypatch)
    app._ensure_singletons()
    assert run(app.overview())["controls_enabled"] is False
    for call in (lambda: app.sim_demo(), lambda: app.approve_intent("x"),
                 lambda: app.sim_failure(app.FailureRequest(kind="link", target=["sw-a", "h1"]))):
        with pytest.raises(HTTPException) as denied:
            run(call())
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as healing:
        run(app.healing())
    assert healing.value.status_code == 501
    sys.modules.pop("viewer.app", None)
