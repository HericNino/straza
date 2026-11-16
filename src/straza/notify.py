"""Notifiers: where alerts go.

A notifier must never take the monitor down with it.  If the webhook
endpoint is unreachable (quite likely during the very outage being
reported), the failure is logged and the monitor carries on.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request
from typing import Any, Dict, Iterable, List

from .config import Config, NotifierConfig

log = logging.getLogger("straza.notify")


def format_alert(kind: str, incident: Dict[str, Any], note: str = "") -> str:
    """One line a tired person can read at 3 a.m.: severity, what, which, why."""
    text = "[%s] %s #%d %s" % (incident["severity"].upper(), kind.upper(),
                               incident["id"], incident["check_name"])
    if incident.get("cause"):
        text += " - " + incident["cause"]
    if note:
        text += " (%s)" % note
    return text


class Notifier:
    def __init__(self, cfg: NotifierConfig):
        self.name = cfg.name

    def send(self, kind: str, incident: Dict[str, Any], note: str = "") -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, kind, incident, note=""):
        print(time.strftime("%H:%M:%S ") + format_alert(kind, incident, note),
              file=sys.stderr, flush=True)


class FileNotifier(Notifier):
    """Appends one JSON object per line - easy to grep, easy to ship elsewhere."""

    def __init__(self, cfg: NotifierConfig):
        super().__init__(cfg)
        self.path = cfg.path

    def send(self, kind, incident, note=""):
        record = {"ts": time.time(), "kind": kind, "note": note,
                  "text": format_alert(kind, incident, note), "incident": incident}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


class WebhookNotifier(Notifier):
    """POSTs JSON.  The top-level ``text`` field is what Slack, Mattermost and
    Discord-compatible incoming webhooks display, so those work unchanged."""

    def __init__(self, cfg: NotifierConfig, timeout: float = 5.0):
        super().__init__(cfg)
        self.url = cfg.url
        self.timeout = timeout

    def send(self, kind, incident, note=""):
        body = json.dumps({"text": format_alert(kind, incident, note), "kind": kind,
                           "note": note, "incident": incident}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "straza/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


_TYPES = {"console": ConsoleNotifier, "file": FileNotifier, "webhook": WebhookNotifier}


def build(config: Config) -> Dict[str, Notifier]:
    return {n.name: _TYPES[n.type](n) for n in config.notifiers}


def dispatch(notifiers: Dict[str, Notifier], names: Iterable[str], kind: str,
             incident: Dict[str, Any], note: str = "") -> List[str]:
    """Send to each named notifier; return the names that failed."""
    failed = []
    for name in names:
        try:
            notifiers[name].send(kind, incident, note)
        except Exception as e:  # noqa: BLE001 - one broken channel must not stop the others
            log.warning("notifier %s failed: %s", name, e)
            failed.append(name)
    return failed
