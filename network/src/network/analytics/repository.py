"""Persistent telemetry storage on SQLite (Submodule 3.1).

Stores telemetry samples, network events and learned baselines in one
SQLite database (a file, or ``":memory:"`` for tests). SQLite ships with
Python, needs no server, and comfortably handles this project's volumes.
All access goes through :class:`TelemetryRepository`, so the storage
engine can be swapped later (e.g. for a time-series database) without
touching the rest of the analytics code.

Timestamps are UNIX epoch seconds (UTC) everywhere.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from network.telemetry.metrics import Metric, Sample

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    metric      TEXT    NOT NULL,
    target      TEXT    NOT NULL,
    target_kind TEXT    NOT NULL,
    value       REAL    NOT NULL,
    source      TEXT    NOT NULL,
    simulated   INTEGER NOT NULL,
    labels      TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_samples_series ON samples (metric, target, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    source  TEXT NOT NULL,
    result  TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (type, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS baselines (
    metric     TEXT NOT NULL,
    target     TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_ts REAL NOT NULL,
    PRIMARY KEY (metric, target)
);
"""


def event_timestamp(event) -> float:
    """Epoch seconds of an :class:`Event` (its timestamp is ISO-8601)."""
    return datetime.fromisoformat(event.timestamp).timestamp()


class TelemetryRepository:
    """SQLite-backed store for samples, events and baselines. Thread-safe."""

    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "TelemetryRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        return row["value"]

    # ------------------------------------------------------------------
    # Samples
    # ------------------------------------------------------------------

    def write_samples(self, samples: Iterable[Sample]) -> int:
        """Insert samples in a single transaction. Returns the count."""
        rows = [
            (
                s.timestamp,
                s.metric.value,
                s.target,
                s.target_kind.value,
                s.value,
                s.source,
                1 if s.simulated else 0,
                json.dumps(s.labels, sort_keys=True),
            )
            for s in samples
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (ts, metric, target, target_kind, value, source, simulated, labels)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def query_samples(
        self,
        metric: Optional[Metric] = None,
        target: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[Sample]:
        """Samples in time order; ``start`` inclusive, ``end`` exclusive."""
        clauses, params = self._filters(metric, target, start, end)
        sql = "SELECT * FROM samples" + clauses + " ORDER BY ts, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_sample(r) for r in rows]

    def query_points(
        self,
        metric: Metric,
        target: str,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> List[Tuple[float, float]]:
        """Lightweight ``(timestamp, value)`` pairs for one series."""
        clauses, params = self._filters(metric, target, start, end)
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, value FROM samples" + clauses + " ORDER BY ts, id", params
            ).fetchall()
        return [(r["ts"], r["value"]) for r in rows]

    def series_keys(self) -> List[Tuple[Metric, str, str]]:
        """Every stored series as ``(metric, target, target_kind)``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT metric, target, target_kind FROM samples ORDER BY metric, target"
            ).fetchall()
        return [(Metric(r["metric"]), r["target"], r["target_kind"]) for r in rows]

    def targets(self, target_kind: Optional[str] = None) -> List[str]:
        sql = "SELECT DISTINCT target FROM samples"
        params: List[Any] = []
        if target_kind is not None:
            sql += " WHERE target_kind = ?"
            params.append(target_kind)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY target", params).fetchall()
        return [r["target"] for r in rows]

    def time_range(self) -> Optional[Tuple[float, float]]:
        with self._lock:
            row = self._conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM samples").fetchone()
        if row["lo"] is None:
            return None
        return (row["lo"], row["hi"])

    def count_samples(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def write_event(self, event) -> None:
        """Persist an :class:`~network.events.event.Event`."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, type, source, result, data) VALUES (?, ?, ?, ?, ?)",
                (
                    event_timestamp(event),
                    event.type.value,
                    event.source,
                    event.result,
                    json.dumps(event.data, sort_keys=True, default=str),
                ),
            )
            self._conn.commit()

    def query_events(
        self,
        types: Optional[Sequence[str]] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Events in time order as dicts (``ts``, ``type``, ``source``, ``result``, ``data``)."""
        clauses: List[str] = []
        params: List[Any] = []
        if types:
            type_values = [getattr(t, "value", t) for t in types]
            clauses.append("type IN (%s)" % ",".join("?" * len(type_values)))
            params.extend(type_values)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY ts, id", params).fetchall()
        return [
            {
                "ts": r["ts"],
                "type": r["type"],
                "source": r["source"],
                "result": r["result"],
                "data": json.loads(r["data"]),
            }
            for r in rows
        ]

    def count_events(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    def save_baseline(self, metric: Metric, target: str, data: Dict[str, Any], created_ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO baselines (metric, target, data, created_ts) VALUES (?, ?, ?, ?)",
                (Metric(metric).value, target, json.dumps(data, sort_keys=True), created_ts),
            )
            self._conn.commit()

    def load_baselines(self) -> Dict[Tuple[Metric, str], Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT metric, target, data FROM baselines").fetchall()
        return {(Metric(r["metric"]), r["target"]): json.loads(r["data"]) for r in rows}

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def purge_before(self, ts: float) -> Dict[str, int]:
        """Delete samples and events older than ``ts``."""
        with self._lock:
            samples = self._conn.execute("DELETE FROM samples WHERE ts < ?", (ts,)).rowcount
            events = self._conn.execute("DELETE FROM events WHERE ts < ?", (ts,)).rowcount
            self._conn.commit()
        return {"samples": samples, "events": events}

    def stats(self) -> Dict[str, Any]:
        rng = self.time_range()
        return {
            "path": self.path,
            "samples": self.count_samples(),
            "events": self.count_events(),
            "series": len(self.series_keys()),
            "start": rng[0] if rng else None,
            "end": rng[1] if rng else None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _filters(metric, target, start, end):
        clauses: List[str] = []
        params: List[Any] = []
        if metric is not None:
            clauses.append("metric = ?")
            params.append(Metric(metric).value)
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _row_to_sample(row) -> Sample:
        return Sample(
            metric=Metric(row["metric"]),
            target=row["target"],
            value=row["value"],
            timestamp=row["ts"],
            source=row["source"],
            simulated=bool(row["simulated"]),
            labels=json.loads(row["labels"]),
        )
