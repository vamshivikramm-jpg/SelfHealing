"""Unit tests for Module 3.1 (TelemetryRepository) and 3.2 (TelemetryRecorder).

SQLite in memory or in a temporary file; standard library only.
"""

import threading

import pytest

from network.analytics.recorder import TelemetryRecorder
from network.analytics.repository import TelemetryRepository, event_timestamp
from network.events import Event, EventBus, EventType
from network.telemetry import Metric, TelemetryCollector, TelemetrySource, make_sample


def s(metric, target, value, ts, simulated=True):
    return make_sample(metric, target, value, ts, "test", simulated, {"src": "a"})


@pytest.fixture()
def repo():
    r = TelemetryRepository()
    yield r
    r.close()


# --------------------------------------------------------------------------
# 3.1 Repository
# --------------------------------------------------------------------------


def test_schema_and_empty_state(repo):
    assert repo.schema_version == "1"
    assert repo.count_samples() == 0
    assert repo.time_range() is None
    assert repo.stats()["series"] == 0


def test_write_and_query_samples_round_trip(repo):
    written = repo.write_samples([s(Metric.LINK_LATENCY_MS, "h1-sw-a", 1.5, 10.0),
                                  s(Metric.NODE_CPU_PCT, "h1", 20, 10.0, simulated=False)])
    assert written == 2
    latency = repo.query_samples(metric=Metric.LINK_LATENCY_MS)[0]
    assert latency.value == 1.5 and latency.target == "h1-sw-a"
    assert latency.simulated is True and latency.labels == {"src": "a"}
    assert repo.query_samples(target="h1")[0].simulated is False
    assert repo.write_samples([]) == 0


def test_time_filters_are_start_inclusive_end_exclusive(repo):
    repo.write_samples([s(Metric.NODE_CPU_PCT, "h1", i, float(i)) for i in range(10)])
    assert [x.value for x in repo.query_samples(start=3, end=6)] == [3.0, 4.0, 5.0]
    assert repo.query_points(Metric.NODE_CPU_PCT, "h1", start=8) == [(8.0, 8.0), (9.0, 9.0)]
    assert len(repo.query_samples(limit=4)) == 4
    assert repo.time_range() == (0.0, 9.0)


def test_series_keys_and_targets(repo):
    repo.write_samples([s(Metric.LINK_STATUS, "h1-sw-a", 1, 1.0), s(Metric.NODE_STATUS, "h1", 1, 1.0)])
    assert repo.series_keys() == [(Metric.LINK_STATUS, "h1-sw-a", "link"), (Metric.NODE_STATUS, "h1", "node")]
    assert repo.targets() == ["h1", "h1-sw-a"]
    assert repo.targets("link") == ["h1-sw-a"]


def test_events_round_trip(repo):
    event = Event(type=EventType.FAILURE_INJECTED, source="failure.manager",
                  timestamp="2026-01-01T00:00:10+00:00", data={"node": "sw-a"})
    repo.write_event(event)
    repo.write_event(Event(type=EventType.HEALTH_CHANGED, source="telemetry.collector",
                           timestamp="2026-01-01T00:00:20+00:00", data={"key": "node:sw-a"}))
    events = repo.query_events()
    assert [e["type"] for e in events] == ["FAILURE_INJECTED", "HEALTH_CHANGED"]
    assert events[0]["data"] == {"node": "sw-a"}
    assert events[0]["ts"] == event_timestamp(event)
    assert [e["type"] for e in repo.query_events(types=[EventType.HEALTH_CHANGED])] == ["HEALTH_CHANGED"]
    assert repo.query_events(start=events[1]["ts"])[0]["type"] == "HEALTH_CHANGED"
    assert repo.count_events() == 2


def test_baselines_round_trip(repo):
    repo.save_baseline(Metric.LINK_LATENCY_MS, "h1-sw-a", {"mean": 1.0}, 5.0)
    repo.save_baseline(Metric.LINK_LATENCY_MS, "h1-sw-a", {"mean": 2.0}, 6.0)  # replaces
    assert repo.load_baselines() == {(Metric.LINK_LATENCY_MS, "h1-sw-a"): {"mean": 2.0}}


def test_purge_before(repo):
    repo.write_samples([s(Metric.NODE_CPU_PCT, "h1", i, float(i)) for i in range(5)])
    repo.write_event(Event(type=EventType.NODE_DISABLED, source="x", timestamp="1970-01-01T00:00:01+00:00"))
    assert repo.purge_before(3.0) == {"samples": 3, "events": 1}
    assert repo.count_samples() == 2


def test_file_database_persists(tmp_path):
    path = tmp_path / "telemetry.db"
    with TelemetryRepository(str(path)) as r:
        r.write_samples([s(Metric.NODE_CPU_PCT, "h1", 7, 1.0)])
    with TelemetryRepository(str(path)) as r:
        assert r.query_samples()[0].value == 7.0


def test_concurrent_writes(repo):
    def writer(target):
        for i in range(200):
            repo.write_samples([s(Metric.NODE_CPU_PCT, target, i, float(i))])

    threads = [threading.Thread(target=writer, args=(f"h{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert repo.count_samples() == 800


# --------------------------------------------------------------------------
# 3.2 Recorder (+ collector listener hook)
# --------------------------------------------------------------------------


class CounterSource(TelemetrySource):
    name = "counter"
    simulated = True

    def collect(self, timestamp):
        return [s(Metric.LINK_BYTES, "h1-sw-a", timestamp * 1000, timestamp),
                s(Metric.LINK_CAPACITY_MBPS, "h1-sw-a", 1000, timestamp),
                s(Metric.LINK_STATUS, "h1-sw-a", 1, timestamp)]


def _collector(bus):
    clock = iter(range(1, 100))
    return TelemetryCollector([CounterSource()], event_bus=bus, clock=lambda: float(next(clock)))


def test_recorder_persists_collections_and_events(repo):
    bus = EventBus()
    collector = _collector(bus)
    recorder = TelemetryRecorder(repo, collector=collector, event_bus=bus).attach()
    for _ in range(3):
        collector.collect_once()
    # 3 raw samples x 3 cycles + derived (throughput, drop? no, utilization) from cycle 2 on
    assert repo.count_samples() == recorder.stats()["samples_written"]
    assert {k[0] for k in repo.series_keys()} >= {Metric.LINK_BYTES, Metric.LINK_THROUGHPUT_MBPS}
    types = [e["type"] for e in repo.query_events()]
    assert "HEALTH_CHANGED" in types
    assert "TELEMETRY_COLLECTED" not in types  # excluded by default
    assert recorder.stats()["errors"] == 0


def test_recorder_detach_stops_recording(repo):
    bus = EventBus()
    collector = _collector(bus)
    recorder = TelemetryRecorder(repo, collector=collector, event_bus=bus).attach()
    recorder.attach()  # idempotent
    collector.collect_once()
    recorder.detach()
    before = repo.count_samples()
    collector.collect_once()
    assert repo.count_samples() == before
    assert recorder.attached is False


def test_recorder_counts_write_errors(repo):
    bus = EventBus()
    collector = _collector(bus)
    recorder = TelemetryRecorder(repo, collector=collector, event_bus=bus).attach()
    repo.close()  # every write now fails
    collector.collect_once()  # must not raise
    assert recorder.stats()["errors"] >= 1


def test_collector_listener_isolation():
    collector = _collector(EventBus())
    seen = []
    collector.add_listener(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    token = collector.add_listener(seen.append)
    collector.collect_once()
    assert len(seen) == 1
    assert collector.remove_listener(token) is True
    assert collector.remove_listener(token) is False
    with pytest.raises(TypeError):
        collector.add_listener("not callable")
