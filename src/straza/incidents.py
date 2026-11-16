"""Incident lifecycle: when to open, when to resolve, when to escalate.

Rules, in plain words:

1. One failed probe is not an incident.  Networks hiccup.  An incident opens
   only after ``failures_to_open`` failures *in a row*.
2. One good probe does not end an incident either.  A service that flaps
   up/down would otherwise open and close an incident every minute and bury
   the on-call person in alerts.  It resolves after ``successes_to_resolve``
   good probes in a row.
3. While an incident is open and nobody has acknowledged it, it climbs the
   escalation ladder: each step fires once, when the incident has been open
   for that step's ``after_minutes``.
4. Acknowledging stops the climb ("someone is on it") but not the incident.
   When it resolves, everyone who was told about it is told it is over.

The opening alert is simply step 0 of the ladder, so there is one code path
for "tell people" instead of two.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import notify
from .checks import DOWN, CheckResult
from .config import CheckConfig, Config
from .store import Store


class IncidentManager:
    def __init__(self, config: Config, store: Store, notifiers: Dict[str, notify.Notifier]):
        self.config = config
        self.store = store
        self.notifiers = notifiers

    def record(self, check: CheckConfig, result: CheckResult, now: float) -> Optional[str]:
        """Store one probe result and apply rules 1 and 2.

        Returns "opened", "resolved" or None.
        """
        self.store.add_result(check.name, now, result.status, result.latency_ms, result.detail)
        incident = self.store.open_incident(check.name)

        if incident is None:
            recent = self.store.last_statuses(check.name, check.failures_to_open)
            if len(recent) == check.failures_to_open and all(s == DOWN for s in recent):
                cause = result.detail
                if check.failures_to_open > 1:
                    cause += " (%d consecutive failures)" % check.failures_to_open
                self.store.create_incident(check.name, check.severity, now, cause)
                self.escalate(now)  # step 0 fires immediately
                return "opened"
            return None

        recent = self.store.last_statuses(check.name, check.successes_to_resolve)
        if len(recent) == check.successes_to_resolve and all(s != DOWN for s in recent):
            duration = _duration(now - incident["opened_at"])
            self.store.resolve_incident(incident["id"], now, "recovered after " + duration)
            incident = self.store.incident(incident["id"])
            notify.dispatch(self.notifiers, self._notified_so_far(incident), "resolved",
                            incident, "down for " + duration)
            return "resolved"
        return None

    def escalate(self, now: float) -> List[int]:
        """Apply rule 3 to every open incident.  Returns ids that escalated."""
        escalated = []
        steps = self.config.escalation
        for incident in self.store.open_incidents():
            if incident["acked_at"] is not None:
                continue
            age_min = (now - incident["opened_at"]) / 60.0
            level = incident["escalation_level"]
            # Fire every step that has come due since the last one we fired.
            # Normally that is at most one, but if the monitor was restarted
            # mid-incident it may be several, and none should be skipped.
            while level + 1 < len(steps) and steps[level + 1].after_minutes <= age_min:
                level += 1
                step = steps[level]
                kind = "opened" if level == 0 else "escalated"
                note = "" if level == 0 else "unacknowledged for %s" % _duration(age_min * 60)
                self.store.set_escalation_level(
                    incident["id"], level, now, "level %d -> %s" % (level, ", ".join(step.notify)))
                fresh = self.store.incident(incident["id"])
                notify.dispatch(self.notifiers, step.notify, kind, fresh, note)
                escalated.append(incident["id"])
        return escalated

    def acknowledge(self, incident_id: int, who: str, now: float) -> bool:
        return self.store.ack_incident(incident_id, now, who)

    def _notified_so_far(self, incident: Dict[str, Any]) -> List[str]:
        names: List[str] = []
        for step in self.config.escalation[: incident["escalation_level"] + 1]:
            for name in step.notify:
                if name not in names:
                    names.append(name)
        return names


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % divmod(seconds, 60)
    hours, rest = divmod(seconds, 3600)
    if hours < 48:
        return "%dh %02dm" % (hours, rest // 60)
    return "%dd %02dh" % divmod(hours, 24)


format_duration = _duration
