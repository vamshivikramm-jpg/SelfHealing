"""Unit tests for Module 3.8 — analytics summaries."""

import pathlib

import pytest

from network.analytics.repository import TelemetryRepository
from network.analytics.scenarios import PlannedFailure, ScenarioPlan, ScenarioRunner
from network.analytics.summary import (
    describe,
    incident_stats,
    incidents,
    network_summary,
    target_summary,
)
from network.config import load_topology_config
from network.events import Event, EventType
from network.telemetry import Metric, make_sample

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = load_topology_config(ROOT / "network" / "config" / "enterprise.yaml")
T0 = 1_700_000_000.0


def _health(repo, key, status, ts):
    from datetime import datetime, timezone

    repo.write_event(Event(type=EventType.HEALTH_CHANGED, source="t",
                           timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                           data={"key": key, "status": status}))


def test_describe():
    assert describe([]) is None
    d = describe([1.0, 2.0, 3.0, 4.0])
    assert d["mean"] == 2.5 and d["max"] == 4.0 and d["min"] == 1.0 and d["samples"] == 4


def test_target_summary_availability():
    repo = TelemetryRepository()
    repo.write_samples([make_sample(Metric.LINK_STATUS, "l", 1 if i < 8 else 0, float(i), "t", True)
                        for i in range(10)])
    repo.write_samples([make_sample(Metric.LINK_LATENCY_MS, "l", 2.0, float(i), "t", True) for i in range(10)])
    summary = target_summary(repo, "l", "link")
    assert summary["availability_pct"] == 80.0
    assert summary["latency_ms"]["mean"] == 2.0
    assert summary["packet_loss_pct"] is None
    assert target_summary(repo, "l", "link", start=0, end=5)["availability_pct"] == 100.0


def test_incidents_and_stats():
    repo = TelemetryRepository()
    _health(repo, "link:a", "HEALTHY", 0)
    _health(repo, "link:a", "DOWN", 100)
    _health(repo, "link:a", "HEALTHY", 160)
    _health(repo, "link:b", "DEGRADED", 200)
    _health(repo, "link:b", "DOWN", 220)       # DEGRADED -> DOWN
    _health(repo, "link:b", "HEALTHY", 260)
    _health(repo, "node:c", "DOWN", 300)       # still open
    down = incidents(repo, end=400)
    assert [(i["key"], i["start"], i["end"]) for i in down] == [
        ("link:a", 100, 160), ("link:b", 220, 260), ("node:c", 300, None)]
    assert down[-1]["duration_s"] == 100
    stats = incident_stats(down, period_s=400)
    assert stats["count"] == 3 and stats["open"] == 1
    assert stats["total_duration_s"] == 200
    assert stats["mttr_s"] == pytest.approx(50.0)   # closed incidents: 60 s and 40 s
    assert stats["mtbf_s"] == pytest.approx((400 - 200) / 3)
    degraded = incidents(repo, statuses=("DEGRADED",))
    assert [(i["key"], i["start"], i["end"]) for i in degraded] == [("link:b", 200, 220)]
    assert incidents(repo, start=170, end=400)[0]["key"] == "link:b"
    assert incident_stats([], None)["mttr_s"] is None


def test_network_summary_from_scenario():
    plan = ScenarioPlan(duration_s=600, step_s=5, seed=1, failures=(
        PlannedFailure("link", ("sw-a", "h1"), 100, 60),
        PlannedFailure("switch", ("sw-b",), 300, 30),
    ))
    result = ScenarioRunner(plan, config=CONFIG, start_time=T0).run()
    summary = network_summary(result.repository)
    assert summary["simulated"] == "simulated"
    assert summary["period_s"] == 600
    h1 = next(t for t in summary["links"]["targets"] if t["target"] == "h1-sw-a")
    # 12 of 121 collections down
    assert h1["availability_pct"] == pytest.approx(109 / 121 * 100)
    assert summary["links"]["worst"][0]["target"] in {"h1-sw-a", "h4-sw-b", "h5-sw-b", "rtr-core-sw-b"}
    down = summary["incidents"]["down"]
    assert down["count"] == 1 + 4      # h1 link; sw-b node + its 3 links
    assert down["mttr_s"] == pytest.approx((60 + 4 * 30) / 5)
    assert summary["nodes"]["availability_pct"] < 100


def test_simulated_mix():
    repo = TelemetryRepository()
    assert repo.simulated_mix() is None
    repo.write_samples([make_sample(Metric.NODE_CPU_PCT, "h", 1, 1.0, "t", False)])
    assert repo.simulated_mix() == "live"
    repo.write_samples([make_sample(Metric.NODE_CPU_PCT, "h", 1, 2.0, "t", True)])
    assert repo.simulated_mix() == "mixed"
