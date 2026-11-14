"""Analytics summaries (Submodule 3.8).

Turns stored telemetry and health history into the numbers an operator
(and Module 9's evaluation) asks for:

- per target: availability %, latency mean/p95/max, loss, utilisation,
  throughput (links) or CPU/memory (nodes);
- incidents: periods during which a target was DOWN (or DEGRADED),
  reconstructed from ``HEALTH_CHANGED`` events;
- incident statistics: count, total time, MTTR (mean time to recovery)
  and MTBF (mean time between failures) over the period;
- a network-wide summary combining all of the above.

Every summary states whether the underlying data is simulated.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

from network.analytics.preprocess import percentile
from network.telemetry.metrics import Metric

LINK_METRICS = {
    "latency_ms": Metric.LINK_LATENCY_MS,
    "packet_loss_pct": Metric.LINK_PACKET_LOSS_PCT,
    "utilization_pct": Metric.LINK_UTILIZATION_PCT,
    "throughput_mbps": Metric.LINK_THROUGHPUT_MBPS,
}
NODE_METRICS = {"cpu_pct": Metric.NODE_CPU_PCT, "memory_pct": Metric.NODE_MEMORY_PCT}


def describe(values: Sequence[float]) -> Optional[dict]:
    """mean / p95 / max / min of a list of numbers, or None if empty."""
    if not values:
        return None
    data = sorted(values)
    return {
        "mean": sum(data) / len(data),
        "p95": percentile(data, 95),
        "max": data[-1],
        "min": data[0],
        "samples": len(data),
    }


def target_summary(repository, target: str, target_kind: str,
                   start: Optional[float] = None, end: Optional[float] = None) -> dict:
    status_metric = Metric.LINK_STATUS if target_kind == "link" else Metric.NODE_STATUS
    status = [v for _, v in repository.query_points(status_metric, target, start, end)]
    metrics = LINK_METRICS if target_kind == "link" else NODE_METRICS
    summary = {
        "target": target,
        "target_kind": target_kind,
        "availability_pct": (sum(status) / len(status) * 100.0) if status else None,
        "status_samples": len(status),
    }
    for name, metric in metrics.items():
        summary[name] = describe([v for _, v in repository.query_points(metric, target, start, end)])
    return summary


def incidents(repository, start: Optional[float] = None, end: Optional[float] = None,
              statuses: Iterable[str] = ("DOWN",)) -> List[dict]:
    """Periods in which a target's health was in ``statuses``.

    Built from ``HEALTH_CHANGED`` events. An incident still open at the end
    of the data has ``end=None`` and its duration counts up to ``end`` (or
    the last event) for statistics.
    """
    wanted = set(statuses)
    events = repository.query_events(types=["HEALTH_CHANGED"], end=end)
    open_since: Dict[str, dict] = {}
    found: List[dict] = []
    for event in events:
        data = event["data"]
        key, status, ts = data["key"], data["status"], event["ts"]
        current = open_since.get(key)
        if current is not None and status not in wanted:
            current["end"] = ts
            current["duration_s"] = ts - current["start"]
            found.append(current)
            del open_since[key]
        elif current is None and status in wanted:
            open_since[key] = {"key": key, "status": status, "start": ts, "end": None, "duration_s": None}
    last = end if end is not None else (events[-1]["ts"] if events else None)
    for incident in open_since.values():
        incident["duration_s"] = (last - incident["start"]) if last is not None else None
        found.append(incident)
    if start is not None:
        found = [i for i in found if i["end"] is None or i["end"] > start]
    return sorted(found, key=lambda i: (i["start"], i["key"]))


def incident_stats(found: Sequence[dict], period_s: Optional[float]) -> dict:
    closed = [i["duration_s"] for i in found if i["end"] is not None]
    total = sum(i["duration_s"] or 0.0 for i in found)
    return {
        "count": len(found),
        "open": sum(1 for i in found if i["end"] is None),
        "total_duration_s": total,
        "mttr_s": (sum(closed) / len(closed)) if closed else None,
        "mtbf_s": ((period_s - total) / len(found)) if (found and period_s) else None,
    }


def network_summary(repository, start: Optional[float] = None, end: Optional[float] = None,
                    worst: int = 3) -> dict:
    rng = repository.time_range()
    lo = start if start is not None else (rng[0] if rng else None)
    hi = end if end is not None else (rng[1] if rng else None)
    period = (hi - lo) if (lo is not None and hi is not None) else None

    links = [target_summary(repository, t, "link", start, end) for t in repository.targets("link")]
    nodes = [target_summary(repository, t, "node", start, end) for t in repository.targets("node")]

    def overall(rows):
        values = [r["availability_pct"] for r in rows if r["availability_pct"] is not None]
        return (sum(values) / len(values)) if values else None

    def worst_of(rows):
        ranked = sorted((r for r in rows if r["availability_pct"] is not None),
                        key=lambda r: r["availability_pct"])
        return [{"target": r["target"], "availability_pct": r["availability_pct"]} for r in ranked[:worst]]

    down = incidents(repository, start, end, ("DOWN",))
    degraded = incidents(repository, start, end, ("DEGRADED",))
    return {
        "start": lo,
        "end": hi,
        "period_s": period,
        "simulated": repository.simulated_mix(),
        "links": {"availability_pct": overall(links), "worst": worst_of(links), "targets": links},
        "nodes": {"availability_pct": overall(nodes), "worst": worst_of(nodes), "targets": nodes},
        "incidents": {
            "down": incident_stats(down, period),
            "degraded": incident_stats(degraded, period),
            "recent": (down + degraded)[-10:],
        },
    }
