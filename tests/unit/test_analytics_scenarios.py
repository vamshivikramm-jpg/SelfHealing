"""Unit tests for Module 3.7 — simulated data plane, scenario runner and
labelled datasets. Uses the real NetworkController and FailureManager on
the in-memory SimulatedNetworkBuilder (no Mininet network needed).
"""

import csv
import json
import pathlib

import pytest

from network.analytics.dataset import (
    LABEL_COLUMNS,
    build_dataset,
    columns_for,
    dataset_metadata,
    label_counts,
    label_row,
    write_csv,
    write_metadata,
)
from network.analytics.features import FeatureExtractor
from network.analytics.generate import main as generate_main
from network.analytics.scenarios import (
    FailureRecord,
    PlannedFailure,
    Precursor,
    ScenarioPlan,
    ScenarioRunner,
    generate_plan,
)
from network.config import load_topology_config
from network.control import NetworkController
from network.failure import FailureManager
from network.simulation.dataplane import SimulatedNetworkBuilder
from network.telemetry import Metric

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = load_topology_config(ROOT / "network" / "config" / "enterprise.yaml")
T0 = 1_700_000_000.0


# --------------------------------------------------------------------------
# Simulated data plane
# --------------------------------------------------------------------------


def test_controller_and_failure_manager_run_on_simulated_dataplane():
    builder = SimulatedNetworkBuilder(config=CONFIG)
    controller = NetworkController(builder=builder)
    controller.start_network()
    manager = FailureManager(controller)
    manager.inject_switch_failure("sw-a")
    assert builder.net.get("sw-a").running is False
    manager.recover_switch("sw-a")
    assert builder.net.get("sw-a").running is True
    manager.disconnect_link("sw-a", "h1")
    assert builder.net.link_status[("h1", "sw-a")] == "down"
    manager.inject_latency("sw-a", "h1", 50)
    assert builder.net.impairments["h1-sw-a"] == {"latency_ms": 50.0}
    controller.stop_network()
    assert builder.net is None


# --------------------------------------------------------------------------
# Plan validation and generation
# --------------------------------------------------------------------------


def test_planned_failure_validation():
    with pytest.raises(ValueError):
        PlannedFailure("meteor", ("sw-a",), 10, 10)
    with pytest.raises(ValueError):
        PlannedFailure("switch", ("sw-a", "h1"), 10, 10)
    with pytest.raises(ValueError):
        PlannedFailure("latency", ("sw-a", "h1"), 10, 10)  # magnitude missing
    with pytest.raises(ValueError):
        PlannedFailure("bandwidth", ("sw-a", "h1"), 100, 10, 5,
                       Precursor("latency", 30, 20, ("sw-a", "h1")))  # incompatible precursor
    with pytest.raises(ValueError):
        PlannedFailure("link", ("sw-a", "h1"), 10, 10, None, Precursor("loss", 30, 1, ("sw-a", "h1")))
    with pytest.raises(ValueError):
        ScenarioPlan(duration_s=50, failures=(PlannedFailure("link", ("sw-a", "h1"), 40, 20),))


def test_generate_plan_is_reproducible_and_sequential():
    a = generate_plan(CONFIG, 7200, seed=4, n_failures=10)
    b = generate_plan(CONFIG, 7200, seed=4, n_failures=10)
    assert a == b
    assert len(a.failures) == 10
    assert a.failures[0].window_start_s >= 300  # healthy warm-up first
    for prev, nxt in zip(a.failures, a.failures[1:]):
        assert nxt.window_start_s >= prev.end_s + 120  # never overlapping
    assert generate_plan(CONFIG, 7200, seed=5, n_failures=10) != a


def test_generate_plan_options():
    none = generate_plan(CONFIG, 3600, seed=1, n_failures=5, precursor_probability=0.0)
    assert all(f.precursor is None for f in none.failures)
    every = generate_plan(CONFIG, 7200, seed=1, n_failures=5, precursor_probability=1.0)
    assert all(f.precursor is not None for f in every.failures)
    only = generate_plan(CONFIG, 3600, seed=1, n_failures=5, kinds=("latency",))
    assert {f.kind for f in only.failures} == {"latency"}
    with pytest.raises(ValueError):
        generate_plan(CONFIG, 600, seed=1, n_failures=20)


# --------------------------------------------------------------------------
# Running scenarios
# --------------------------------------------------------------------------


def _run(failures, duration=600.0):
    plan = ScenarioPlan(duration_s=duration, step_s=5.0, failures=tuple(failures), seed=3)
    return ScenarioRunner(plan, config=CONFIG, start_time=T0).run()


def test_run_records_telemetry_and_events():
    result = _run([PlannedFailure("link", ("sw-a", "h1"), 100, 50)])
    repo = result.repository
    assert result.collections == 121
    assert repo.time_range() == (T0, T0 + 600)
    types = {e["type"] for e in repo.query_events()}
    assert {"NETWORK_STARTED", "LINK_DISCONNECTED", "LINK_CONNECTED", "HEALTH_CHANGED"} <= types
    # collector events carry the virtual clock, not wall-clock time
    health = repo.query_events(types=["HEALTH_CHANGED"])
    assert all(T0 <= e["ts"] <= T0 + 600 for e in health)
    status = repo.query_points(Metric.LINK_STATUS, "h1-sw-a", start=T0 + 100, end=T0 + 150)
    assert {v for _, v in status} == {0.0}
    assert repo.query_points(Metric.LINK_STATUS, "h1-sw-a", start=T0 + 150, end=T0 + 151)[0][1] == 1.0


def test_switch_failure_ground_truth_and_detection():
    result = _run([PlannedFailure("switch", ("sw-a",), 100, 60)])
    record = result.records[0]
    assert record.affected[0] == "node:sw-a"
    assert set(record.affected[1:]) == {"link:h1-sw-a", "link:h2-sw-a", "link:h3-sw-a",
                                        "link:rtr-core-sw-a", "link:srv-db-1-sw-a", "link:srv-web-1-sw-a"}
    assert (record.start, record.end) == (T0 + 100, T0 + 160)
    assert record.detection_delay_s == 0.0  # simulated telemetry reacts in the same collection
    assert result.summary()["detected"] == 1


def test_latency_precursor_ramps_before_failure_and_is_cleared():
    precursor = Precursor("latency", 60, 20.0, ("sw-a", "h1"))
    result = _run([PlannedFailure("link", ("sw-a", "h1"), 200, 50, None, precursor)])
    points = dict(result.repository.query_points(Metric.LINK_LATENCY_MS, "h1-sw-a"))
    assert points[T0 + 135] < 1.0                                # before the ramp
    assert 3.0 < points[T0 + 150] < 6.0                          # early in the ramp (5/60 * 20 * ...)
    assert 19.0 < points[T0 + 195] < 21.0                        # peak just before the failure
    assert points[T0 + 260] < 1.0                                # cleared after recovery
    record = result.records[0]
    assert record.precursor_start == T0 + 140 and record.precursor_kind == "latency"
    assert record.warned_at is None  # 20 ms stays under the 50 ms health threshold


def test_bandwidth_precursor_on_uplink_can_warn_early():
    precursor = Precursor("bandwidth", 60, 40.0, ("rtr-core", "sw-a"))
    result = _run([PlannedFailure("bandwidth", ("rtr-core", "sw-a"), 200, 50, 5, precursor)])
    capacity = dict(result.repository.query_points(Metric.LINK_CAPACITY_MBPS, "rtr-core-sw-a"))
    assert capacity[T0 + 135] == 1000.0
    assert capacity[T0 + 195] == pytest.approx(40.0)
    assert capacity[T0 + 200] == 5.0
    assert capacity[T0 + 260] == 1000.0
    record = result.records[0]
    assert record.warned_at is not None and record.precursor_start <= record.warned_at < record.start


def test_impairment_failures_detected():
    result = _run([
        PlannedFailure("latency", ("sw-b", "h4"), 60, 40, 150),
        PlannedFailure("packet_loss", ("sw-b", "h5"), 200, 60, 25),
        PlannedFailure("bandwidth", ("sw-a", "h2"), 400, 40, 5),
    ], duration=600)
    assert [r.detection_delay_s for r in result.records] == [0.0, 0.0, 0.0]


# --------------------------------------------------------------------------
# Labels and datasets
# --------------------------------------------------------------------------


def _record(**kw):
    base = dict(kind="link", target=("sw-a", "h1"), affected=("link:h1-sw-a",), start=100.0, end=150.0,
                precursor_start=70.0, precursor_kind="latency")
    base.update(kw)
    return FailureRecord(**base)


@pytest.mark.parametrize(
    "t,active,imminent,ttf,precursor",
    [
        (30.0, 0, 0, None, 0),     # failure 70 s away > horizon 60
        (45.0, 0, 1, 55.0, 0),     # within horizon
        (75.0, 0, 1, 25.0, 1),     # precursor ramp, failure 25 s away
        (100.0, 0, 1, 0.0, 1),     # last bucket [95,100) ends just as failure starts
        (105.0, 1, 0, None, 0),    # first bucket containing the failure
        (150.0, 1, 0, None, 0),    # last bucket [145,150) before recovery
        (155.0, 0, 0, None, 0),    # recovered at 150, before that collection
        (160.0, 0, 0, None, 0),    # recovered
    ],
)
def test_label_row_timing(t, active, imminent, ttf, precursor):
    row = label_row({"timestamp": t, "target": "h1-sw-a"}, "link", [_record()], horizon_s=60, step_s=5)
    assert row["label_failure_active"] == active
    assert row["label_failure_type"] == ("link" if active else "none")
    assert row["label_failure_imminent"] == imminent
    assert row["label_time_to_failure_s"] == ttf
    assert row["label_precursor_active"] == precursor


def test_label_row_other_target_unaffected():
    row = label_row({"timestamp": 120.0, "target": "h2-sw-a"}, "link", [_record()], 60, 5)
    assert row["label_failure_active"] == 0 and row["label_failure_imminent"] == 0


def test_build_dataset_and_export(tmp_path):
    precursor = Precursor("latency", 60, 20.0, ("sw-a", "h1"))
    result = _run([PlannedFailure("link", ("sw-a", "h1"), 300, 60, None, precursor)], duration=600)
    extractor = FeatureExtractor(step_s=5, windows_s=(30, 60))
    datasets = build_dataset(result, extractor, horizon_s=60)
    links, nodes = datasets["link"], datasets["node"]
    assert len({r["target"] for r in links}) == 9 and len({r["target"] for r in nodes}) == 10
    h1 = [r for r in links if r["target"] == "h1-sw-a"]
    assert sum(r["label_failure_active"] for r in h1) == 12  # 60 s failure = 12 buckets
    assert sum(r["label_failure_imminent"] for r in h1) == 12
    assert all(r["label_failure_active"] == 0 for r in links if r["target"] != "h1-sw-a")
    # the precursor is visible in the features before the failure
    before = next(r for r in h1 if r["timestamp"] == T0 + 300)
    assert before["link.latency_ms__30s__slope"] > 0

    columns = columns_for(links)
    assert columns[:3] == ["timestamp", "target_kind", "target"]
    assert columns[-len(LABEL_COLUMNS):] == list(LABEL_COLUMNS)
    path = tmp_path / "links.csv"
    assert write_csv(links, str(path)) == len(links)
    with open(path) as fh:
        assert len(list(csv.DictReader(fh))) == len(links)

    meta = dataset_metadata(result, datasets, extractor, 60)
    assert meta["simulated"] is True and meta["synthetic_precursors"] is True
    assert "not that real networks" in meta["warning"]
    assert meta["datasets"]["link"]["labels"] == label_counts(links)
    write_metadata(meta, str(tmp_path / "metadata.json"))
    assert json.loads((tmp_path / "metadata.json").read_text())["horizon_s"] == 60


def test_generate_cli(tmp_path, capsys):
    out = tmp_path / "run"
    assert generate_main(["--out", str(out), "--duration", "1200", "--failures", "2", "--seed", "2",
                          "--windows", "30,60", "--keep-db"]) == 0
    assert {p.name for p in out.iterdir()} == {"links.csv", "nodes.csv", "metadata.json", "telemetry.db"}
    summary = json.loads(capsys.readouterr().out)
    assert summary["simulated"] is True and summary["scenario"]["failures"] == 2
