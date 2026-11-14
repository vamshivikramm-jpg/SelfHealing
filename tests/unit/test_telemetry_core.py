"""Unit tests for Module 2 telemetry building blocks.

Covers the data model (2.1), store and rate derivation (2.2) and health
evaluation (2.4). Standard library only; runs anywhere.
"""

import threading

import pytest

from network.telemetry import (
    METRIC_SPECS,
    HealthEvaluator,
    HealthStatus,
    Metric,
    MetricKind,
    RateDeriver,
    Sample,
    TargetKind,
    TelemetryStore,
    Thresholds,
    make_sample,
)
from network.telemetry.health import report_key


def s(metric, target, value, ts, source="test", simulated=True):
    return make_sample(metric, target, value, ts, source, simulated)


# --------------------------------------------------------------------------
# 2.1 Data model
# --------------------------------------------------------------------------


def test_every_metric_has_a_spec():
    assert set(METRIC_SPECS) == set(Metric)


def test_metric_kinds():
    assert METRIC_SPECS[Metric.LINK_BYTES].kind == MetricKind.COUNTER
    assert METRIC_SPECS[Metric.LINK_LATENCY_MS].kind == MetricKind.GAUGE
    assert METRIC_SPECS[Metric.LINK_THROUGHPUT_MBPS].kind == MetricKind.DERIVED


def test_sample_properties_and_to_dict():
    sample = make_sample("link.latency_ms", "h1-sw-a", 3, 1000.0, "sim", True, {"src": "h1"})
    assert sample.metric is Metric.LINK_LATENCY_MS
    assert sample.value == 3.0 and isinstance(sample.value, float)
    assert sample.target_kind is TargetKind.LINK
    assert sample.unit == "ms"
    assert sample.key == (Metric.LINK_LATENCY_MS, "h1-sw-a")
    data = sample.to_dict()
    assert data["metric"] == "link.latency_ms"
    assert data["target_kind"] == "link"
    assert data["simulated"] is True
    assert data["source"] == "sim"
    assert data["labels"] == {"src": "h1"}
    assert data["time"].startswith("1970-01-01T00:16:40")


def test_sample_is_immutable():
    sample = s(Metric.NODE_CPU_PCT, "h1", 5, 1.0)
    with pytest.raises(Exception):
        sample.value = 6


def test_unknown_metric_rejected():
    with pytest.raises(ValueError):
        make_sample("link.nope", "x", 1, 1.0, "t", True)


# --------------------------------------------------------------------------
# 2.2 Store
# --------------------------------------------------------------------------


def test_store_latest_and_series():
    store = TelemetryStore(max_points_per_series=3)
    for i in range(5):
        store.add([s(Metric.NODE_CPU_PCT, "h1", i, float(i)), s(Metric.NODE_CPU_PCT, "h2", 10 + i, float(i))])

    series = store.series(Metric.NODE_CPU_PCT, "h1")
    assert [x.value for x in series] == [2.0, 3.0, 4.0]  # ring buffer keeps last 3
    assert store.latest_value(Metric.NODE_CPU_PCT, "h2") == 14.0
    assert [x.target for x in store.latest()] == ["h1", "h2"]
    assert store.latest(target="h2")[0].value == 14.0
    assert store.latest(metric=Metric.LINK_STATUS) == []
    assert store.latest_value(Metric.LINK_STATUS, "h1") is None


def test_store_series_since_and_limit():
    store = TelemetryStore()
    store.add([s(Metric.NODE_CPU_PCT, "h1", i, float(i)) for i in range(10)])
    assert [x.value for x in store.series(Metric.NODE_CPU_PCT, "h1", since=7)] == [7.0, 8.0, 9.0]
    assert [x.value for x in store.series(Metric.NODE_CPU_PCT, "h1", limit=2)] == [8.0, 9.0]
    assert store.series(Metric.NODE_CPU_PCT, "h1", limit=0) == []
    assert store.series(Metric.NODE_CPU_PCT, "missing") == []


def test_store_stats_keys_and_clear():
    store = TelemetryStore(max_points_per_series=2)
    store.add([s(Metric.NODE_CPU_PCT, "h1", i, float(i)) for i in range(3)])
    store.add([s(Metric.LINK_STATUS, "h1-sw-a", 1, 0.0)])
    assert store.stats() == {"series": 2, "points": 3, "total_added": 4, "max_points_per_series": 2}
    assert store.targets() == ["h1", "h1-sw-a"]
    assert store.series_keys() == [(Metric.LINK_STATUS, "h1-sw-a"), (Metric.NODE_CPU_PCT, "h1")]
    store.clear()
    assert store.stats()["series"] == 0


def test_store_rejects_bad_size():
    with pytest.raises(ValueError):
        TelemetryStore(max_points_per_series=0)


def test_store_is_thread_safe():
    store = TelemetryStore(max_points_per_series=10_000)

    def writer(target):
        for i in range(500):
            store.add([s(Metric.NODE_CPU_PCT, target, i, float(i))])

    threads = [threading.Thread(target=writer, args=(f"h{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.stats()["points"] == 2000


# --------------------------------------------------------------------------
# 2.2 Rate derivation
# --------------------------------------------------------------------------


def _counter_batch(ts, byte_count, dropped=0, capacity=100.0, target="h1-sw-a"):
    return [
        s(Metric.LINK_BYTES, target, byte_count, ts),
        s(Metric.LINK_DROPPED_PACKETS, target, dropped, ts),
        s(Metric.LINK_CAPACITY_MBPS, target, capacity, ts),
    ]


def _by_metric(samples):
    return {x.metric: x for x in samples}


def test_first_batch_produces_no_rates():
    assert RateDeriver().process(_counter_batch(0.0, 0)) == []


def test_throughput_utilization_and_drop_rate():
    deriver = RateDeriver()
    deriver.process(_counter_batch(0.0, 0, 0))
    # 12.5 MB over 10 s = 10 Mbps; 50 drops over 10 s = 5 pps; 10 % of 100 Mbps
    derived = _by_metric(deriver.process(_counter_batch(10.0, 12_500_000, 50)))
    assert derived[Metric.LINK_THROUGHPUT_MBPS].value == pytest.approx(10.0)
    assert derived[Metric.LINK_DROP_RATE_PPS].value == pytest.approx(5.0)
    assert derived[Metric.LINK_UTILIZATION_PCT].value == pytest.approx(10.0)
    assert derived[Metric.LINK_THROUGHPUT_MBPS].simulated is True


def test_utilization_capped_at_100():
    deriver = RateDeriver()
    deriver.process(_counter_batch(0.0, 0, capacity=1.0))
    derived = _by_metric(deriver.process(_counter_batch(1.0, 1_000_000, capacity=1.0)))
    assert derived[Metric.LINK_UTILIZATION_PCT].value == 100.0


def test_counter_reset_skips_one_interval():
    deriver = RateDeriver()
    deriver.process(_counter_batch(0.0, 1_000_000))
    assert _by_metric(deriver.process(_counter_batch(1.0, 10))).get(Metric.LINK_THROUGHPUT_MBPS) is None
    assert deriver.resets_detected == 1
    derived = _by_metric(deriver.process(_counter_batch(2.0, 125_010)))
    assert derived[Metric.LINK_THROUGHPUT_MBPS].value == pytest.approx(1.0)


def test_non_increasing_time_skipped():
    deriver = RateDeriver()
    deriver.process(_counter_batch(5.0, 0))
    assert deriver.process(_counter_batch(5.0, 100)) == []


# --------------------------------------------------------------------------
# 2.4 Health evaluation
# --------------------------------------------------------------------------


def _link(ts=1.0, status=1, latency=0.1, loss=0.0, util=None, target="h1-sw-a"):
    out = [
        s(Metric.LINK_STATUS, target, status, ts),
        s(Metric.LINK_LATENCY_MS, target, latency, ts),
        s(Metric.LINK_PACKET_LOSS_PCT, target, loss, ts),
    ]
    if util is not None:
        out.append(s(Metric.LINK_UTILIZATION_PCT, target, util, ts))
    return out


def test_healthy_link():
    report = HealthEvaluator().evaluate(_link())["link:h1-sw-a"]
    assert report.status is HealthStatus.HEALTHY
    assert report.reasons == ()
    assert report.metrics["link.latency_ms"] == 0.1


def test_down_link():
    report = HealthEvaluator().evaluate(_link(status=0))["link:h1-sw-a"]
    assert report.status is HealthStatus.DOWN


@pytest.mark.parametrize(
    "kwargs,needle",
    [({"latency": 120.0}, "latency"), ({"loss": 10.0}, "packet loss"), ({"util": 99.0}, "utilization")],
)
def test_degraded_link(kwargs, needle):
    report = HealthEvaluator().evaluate(_link(**kwargs))["link:h1-sw-a"]
    assert report.status is HealthStatus.DEGRADED
    assert any(needle in r for r in report.reasons)


def test_custom_thresholds():
    evaluator = HealthEvaluator(Thresholds(latency_ms=200.0))
    assert evaluator.evaluate(_link(latency=120.0))["link:h1-sw-a"].status is HealthStatus.HEALTHY


def test_unknown_without_status():
    report = HealthEvaluator().evaluate([s(Metric.NODE_CPU_PCT, "h1", 10, 1.0)])["node:h1"]
    assert report.status is HealthStatus.UNKNOWN


def test_node_health():
    samples = [
        s(Metric.NODE_STATUS, "srv-db-1", 1, 1.0),
        s(Metric.NODE_CPU_PCT, "srv-db-1", 95, 1.0),
        s(Metric.NODE_MEMORY_PCT, "srv-db-1", 40, 1.0),
    ]
    report = HealthEvaluator().evaluate(samples)[report_key(TargetKind.NODE, "srv-db-1")]
    assert report.status is HealthStatus.DEGRADED
    assert report.target_kind is TargetKind.NODE
    assert report.to_dict()["status"] == "DEGRADED"


def test_evaluator_uses_latest_sample():
    samples = _link(ts=1.0, latency=500.0) + _link(ts=2.0, latency=0.1)
    assert HealthEvaluator().evaluate(samples)["link:h1-sw-a"].status is HealthStatus.HEALTHY
