"""SQLite storage.

Every question the tool answers ("what was uptime last month?", "how long do
incidents take to resolve?") is a SQL query in this file, so the numbers on
the status page, in the CLI and in the report can never disagree.

One connection is shared between the scheduler thread and the web server
threads, guarded by a lock.  WAL mode lets a second *process* (for example
``straza ack 12`` typed in another terminal) write while the monitor runs.
Timestamps are Unix seconds (UTC) - unambiguous and trivially comparable.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY,
    check_name  TEXT NOT NULL,
    ts          REAL NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('up', 'degraded', 'down')),
    latency_ms  REAL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_check_ts ON results (check_name, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id                INTEGER PRIMARY KEY,
    check_name        TEXT NOT NULL,
    severity          TEXT NOT NULL,
    opened_at         REAL NOT NULL,
    acked_at          REAL,
    acked_by          TEXT,
    resolved_at       REAL,
    cause             TEXT,
    escalation_level  INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents (check_name) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS incident_events (
    id           INTEGER PRIMARY KEY,
    incident_id  INTEGER NOT NULL REFERENCES incidents (id),
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _all(self, sql: str, params: Any = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._db:  # "with db" commits, or rolls back on error
            return self._db.execute(sql, params).lastrowid

    # ------------------------------------------------------------------
    # results

    def add_result(self, check_name: str, ts: float, status: str,
                   latency_ms: Optional[float], detail: str) -> None:
        self._write(
            "INSERT INTO results (check_name, ts, status, latency_ms, detail) VALUES (?, ?, ?, ?, ?)",
            (check_name, ts, status, latency_ms, detail))

    def add_results(self, rows) -> None:
        """Bulk insert of (check_name, ts, status, latency_ms, detail) tuples
        in a single transaction - thousands of rows, one fsync."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO results (check_name, ts, status, latency_ms, detail) "
                "VALUES (?, ?, ?, ?, ?)", rows)

    def last_statuses(self, check_name: str, n: int) -> List[str]:
        """The n most recent statuses, newest first."""
        rows = self._all(
            "SELECT status FROM results WHERE check_name = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (check_name, n))
        return [r["status"] for r in rows]

    def latest(self) -> Dict[str, Dict[str, Any]]:
        """The most recent result of every check, keyed by check name."""
        rows = self._all("""
            SELECT r.* FROM results r
            JOIN (SELECT check_name, MAX(id) AS id FROM results GROUP BY check_name) last
              ON last.id = r.id""")
        return {r["check_name"]: r for r in rows}

    def summary(self, since: float, until: float) -> Dict[str, Dict[str, Any]]:
        """Per-check totals, uptime percentage and p95 latency for a window.

        Uptime is the share of probes that did not come back ``down``.
        p95 uses the nearest-rank definition: sort the latencies, take the
        value at position ceil(0.95 * n).  SQLite has no percentile function,
        so a window function numbers the rows and integer arithmetic does the
        ceiling: (n * 95 + 99) / 100.
        """
        rows = self._all("""
            WITH win AS (
                SELECT * FROM results WHERE ts >= ? AND ts < ?
            ),
            totals AS (
                SELECT check_name,
                       COUNT(*)                                        AS total,
                       SUM(status = 'down')                            AS down,
                       SUM(status = 'degraded')                        AS degraded,
                       AVG(latency_ms)                                 AS avg_latency_ms
                FROM win GROUP BY check_name
            ),
            ranked AS (
                SELECT check_name, latency_ms,
                       ROW_NUMBER() OVER (PARTITION BY check_name ORDER BY latency_ms) AS rn,
                       COUNT(*)     OVER (PARTITION BY check_name)                     AS n
                FROM win WHERE latency_ms IS NOT NULL
            ),
            p95 AS (
                SELECT check_name, latency_ms AS p95_latency_ms
                FROM ranked WHERE rn = (n * 95 + 99) / 100
            )
            SELECT t.*, p.p95_latency_ms,
                   100.0 * (t.total - t.down) / t.total AS uptime_pct
            FROM totals t LEFT JOIN p95 p USING (check_name)""", (since, until))
        return {r["check_name"]: r for r in rows}

    def timeline(self, since: float, until: float, buckets: int) -> Dict[str, List[Dict[str, int]]]:
        """Split the window into equal buckets and count statuses in each.

        Returns, per check, a list of ``buckets`` dicts in time order.  A
        bucket with ``total == 0`` means the monitor has no data for it,
        which is different from "everything was fine".
        """
        width = (until - since) / buckets
        rows = self._all("""
            SELECT check_name,
                   CAST((ts - ?) / ? AS INTEGER)  AS bucket,
                   COUNT(*)                       AS total,
                   SUM(status = 'down')           AS down,
                   SUM(status = 'degraded')       AS degraded
            FROM results
            WHERE ts >= ? AND ts < ?
            GROUP BY check_name, bucket""", (since, width, since, until))
        out: Dict[str, List[Dict[str, int]]] = {}
        for r in rows:
            series = out.setdefault(
                r["check_name"],
                [{"total": 0, "down": 0, "degraded": 0} for _ in range(buckets)])
            index = min(r["bucket"], buckets - 1)
            series[index] = {"total": r["total"], "down": r["down"], "degraded": r["degraded"]}
        return out

    def prune(self, older_than: float) -> int:
        """Delete raw results older than the retention limit.  Incidents stay."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM results WHERE ts < ?", (older_than,)).rowcount

    # ------------------------------------------------------------------
    # incidents

    def open_incident(self, check_name: str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT * FROM incidents WHERE check_name = ? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (check_name,))

    def open_incidents(self) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY opened_at")

    def incident(self, incident_id: int) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM incidents WHERE id = ?", (incident_id,))

    def incidents(self, since: float = 0.0, limit: int = 50) -> List[Dict[str, Any]]:
        """Incidents that were open at any point after ``since``, newest first."""
        return self._all(
            "SELECT * FROM incidents WHERE resolved_at IS NULL OR resolved_at >= ? "
            "ORDER BY opened_at DESC LIMIT ?", (since, limit))

    def create_incident(self, check_name: str, severity: str, ts: float, cause: str) -> int:
        with self._lock, self._db:
            incident_id = self._db.execute(
                "INSERT INTO incidents (check_name, severity, opened_at, cause) VALUES (?, ?, ?, ?)",
                (check_name, severity, ts, cause)).lastrowid
            self._db.execute(
                "INSERT INTO incident_events (incident_id, ts, kind, message) VALUES (?, ?, 'opened', ?)",
                (incident_id, ts, cause))
            return incident_id

    def resolve_incident(self, incident_id: int, ts: float, message: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE incidents SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
                (ts, incident_id))
            self._db.execute(
                "INSERT INTO incident_events (incident_id, ts, kind, message) VALUES (?, ?, 'resolved', ?)",
                (incident_id, ts, message))

    def ack_incident(self, incident_id: int, ts: float, who: str) -> bool:
        """Acknowledge an open, not yet acknowledged incident.

        The WHERE clause makes this safe when two people acknowledge at the
        same moment: only one UPDATE matches a row.
        """
        with self._lock, self._db:
            changed = self._db.execute(
                "UPDATE incidents SET acked_at = ?, acked_by = ? "
                "WHERE id = ? AND acked_at IS NULL AND resolved_at IS NULL",
                (ts, who, incident_id)).rowcount
            if changed:
                self._db.execute(
                    "INSERT INTO incident_events (incident_id, ts, kind, message) "
                    "VALUES (?, ?, 'acknowledged', ?)", (incident_id, ts, "by " + who))
            return bool(changed)

    def set_escalation_level(self, incident_id: int, level: int, ts: float, message: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE incidents SET escalation_level = ? WHERE id = ?",
                             (level, incident_id))
            self._db.execute(
                "INSERT INTO incident_events (incident_id, ts, kind, message) "
                "VALUES (?, ?, 'escalated', ?)", (incident_id, ts, message))

    def events(self, incident_id: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY ts, id", (incident_id,))

    def incident_stats(self, since: float, until: float) -> Dict[str, Dict[str, Any]]:
        """Per-check incident count, downtime, MTTA and MTTR for a window.

        Downtime only counts the part of each incident inside the window, so
        an incident that started last month and ended this month is split
        correctly between the two reports.  An incident that is still open is
        counted up to ``until``.

        MTTA = mean time to acknowledge, MTTR = mean time to resolve, both
        measured from the moment the incident opened.
        """
        rows = self._all("""
            SELECT check_name,
                   COUNT(*)                                             AS incidents,
                   SUM(MIN(COALESCE(resolved_at, :until), :until)
                       - MAX(opened_at, :since))                       AS downtime_s,
                   AVG(acked_at - opened_at)                            AS mtta_s,
                   AVG(resolved_at - opened_at)                         AS mttr_s
            FROM incidents
            WHERE opened_at < :until AND COALESCE(resolved_at, :until) > :since
            GROUP BY check_name""", {"since": since, "until": until})
        return {r["check_name"]: r for r in rows}
