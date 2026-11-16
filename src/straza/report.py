"""Availability report in Markdown: the document you send after the month ends."""
from __future__ import annotations

import time
from typing import List

from .config import Config
from .incidents import format_duration
from .store import Store


def build(config: Config, store: Store, now: float, days: int) -> str:
    since = now - days * 86400
    summary = store.summary(since, now)
    stats = store.incident_stats(since, now)

    lines: List[str] = [
        "# Availability report",
        "",
        "Period: %s to %s (%d day%s)" % (_day(since), _day(now), days, "" if days == 1 else "s"),
        "",
        "| Check | Severity | Uptime | Probes | Incidents | Downtime | MTTA | MTTR | p95 latency |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in config.checks:
        s = summary.get(c.name)
        i = stats.get(c.name, {})
        if s is None:
            lines.append("| %s | %s | no data | 0 | 0 | - | - | - | - |" % (c.name, c.severity))
            continue
        lines.append("| %s | %s | %.3f%% | %d | %d | %s | %s | %s | %s |" % (
            c.name, c.severity, s["uptime_pct"], s["total"], i.get("incidents", 0),
            _dur(i.get("downtime_s")), _dur(i.get("mtta_s")), _dur(i.get("mttr_s")),
            "%.0f ms" % s["p95_latency_ms"] if s["p95_latency_ms"] is not None else "-"))

    lines += [
        "",
        "Uptime is the share of probes that succeeded. Downtime is the total length of",
        "incidents inside the period. MTTA and MTTR are the mean time from an incident",
        "opening to it being acknowledged and resolved.",
        "",
        "## Incidents",
        "",
    ]
    incidents = [i for i in store.incidents(since=since, limit=1000) if i["opened_at"] < now]
    if not incidents:
        lines.append("None in this period.")
    else:
        lines += ["| # | Check | Severity | Opened | Duration | Acknowledged by | Cause |",
                  "|---:|---|---|---|---:|---|---|"]
        for inc in reversed(incidents):  # oldest first reads like a story
            duration = (format_duration(inc["resolved_at"] - inc["opened_at"])
                        if inc["resolved_at"] else "still open")
            lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
                inc["id"], inc["check_name"], inc["severity"],
                time.strftime("%Y-%m-%d %H:%M", time.localtime(inc["opened_at"])),
                duration, inc["acked_by"] or "-", (inc["cause"] or "").replace("|", "/")))
    return "\n".join(lines) + "\n"


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _dur(seconds) -> str:
    return format_duration(seconds) if seconds is not None else "-"
