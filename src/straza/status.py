"""One snapshot of "how are things right now", shared by every front end.

The status page, the JSON API and ``straza status`` all render this same
structure, so they cannot show different numbers.
"""
from __future__ import annotations

from typing import Any, Dict

from .config import Config
from .store import Store

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
BUCKETS = 60


def snapshot(config: Config, store: Store, now: float, window: str = "24h") -> Dict[str, Any]:
    window_s = WINDOWS.get(window, WINDOWS["24h"])
    since = now - window_s
    latest = store.latest()
    summary = store.summary(since, now)
    timeline = store.timeline(since, now, BUCKETS)
    open_by_check = {i["check_name"]: i for i in store.open_incidents()}

    checks = []
    for c in config.checks:
        last = latest.get(c.name)
        incident = open_by_check.get(c.name)
        if incident is not None:
            # During an open incident the check is "down" even if the very
            # latest probe happened to pass: it has not proven itself yet.
            status = "down"
        elif last is None:
            status = "unknown"
        else:
            status = last["status"]
        stats = summary.get(c.name, {})
        checks.append({
            "name": c.name,
            "type": c.type,
            "target": c.target,
            "severity": c.severity,
            "status": status,
            "detail": last["detail"] if last else "no data yet",
            "last_checked": last["ts"] if last else None,
            "latency_ms": last["latency_ms"] if last else None,
            "uptime_pct": stats.get("uptime_pct"),
            "p95_latency_ms": stats.get("p95_latency_ms"),
            "probes": stats.get("total", 0),
            "timeline": timeline.get(c.name) or [
                {"total": 0, "down": 0, "degraded": 0} for _ in range(BUCKETS)],
            "incident_id": incident["id"] if incident else None,
        })

    return {
        "generated_at": now,
        "window": window if window in WINDOWS else "24h",
        "window_s": window_s,
        "bucket_s": window_s / BUCKETS,
        "overall": overall(checks),
        "checks": checks,
        "open_incidents": list(open_by_check.values()),
        "recent_incidents": store.incidents(since=since, limit=20),
    }


def overall(checks) -> str:
    states = {c["status"] for c in checks}
    if "down" in states:
        return "down"
    if "degraded" in states:
        return "degraded"
    if states == {"unknown"} or not states:
        return "unknown"
    return "up"
