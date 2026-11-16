"""`straza demo`: a self-contained playground.

Starts a small fake "product" on localhost with one endpoint that is healthy,
one that goes down on a schedule and one that is sometimes slow, then points
a real monitor at it with second-scale intervals.  Within a minute you see an
incident open, escalate to the webhook because nobody acknowledged it, and
resolve on its own.

To make the status page worth looking at from the first second, 24 hours of
synthetic history is written to the (temporary) database first.
"""
from __future__ import annotations

import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod
from .config import Config
from .store import Store

UP_SECONDS, DOWN_SECONDS = 50, 25


def start_target() -> ThreadingHTTPServer:
    """The fake product being monitored.  Listens on a free port."""
    started = time.time()
    rng = random.Random(7)

    class Target(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/checkout":
                phase = (time.time() - started) % (UP_SECONDS + DOWN_SECONDS)
                if phase >= UP_SECONDS:
                    return self._reply(503, "upstream unavailable")
                return self._reply(200, "ok")
            if self.path == "/search":
                time.sleep(rng.choice([0.02, 0.03, 0.05, 0.08, 0.18, 0.22]))
                return self._reply(200, "ok")
            return self._reply(200, "ok")

        def do_POST(self):  # stands in for a Slack / on-call webhook
            import json
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                text = json.loads(body)["text"]
            except (ValueError, KeyError):
                text = body.decode("utf-8", "replace")
            print("%s   webhook received: %s" % (time.strftime("%H:%M:%S"), text), flush=True)
            self._reply(200, "ok")

        def _reply(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True, name="demo-target").start()
    return server


def build_config(target_port: int, database: str, web_port: int) -> Config:
    return config_mod.parse(raw_config(target_port, database, web_port))


def raw_config(target_port: int, database: str, web_port: int) -> dict:
    """The demo's config as plain JSON-able data, so it can also be written
    to disk and used with the other commands (`straza ack -c ...`)."""
    base = "http://127.0.0.1:%d" % target_port
    return {
        "database": database,
        "web": {"port": web_port, "title": "Demo shop status", "allow_ack": True},
        "defaults": {"interval": 2, "timeout": 2, "failures_to_open": 3, "successes_to_resolve": 2},
        "checks": [
            {"name": "checkout-api", "type": "http", "url": base + "/checkout",
             "expect_body": "ok", "severity": "sev1"},
            {"name": "search", "type": "http", "url": base + "/search",
             "max_latency_ms": 150, "severity": "sev2"},
            {"name": "homepage", "type": "http", "url": base + "/", "severity": "sev2"},
            {"name": "app-port", "type": "tcp", "host": "127.0.0.1", "port": target_port,
             "severity": "sev3"},
        ],
        "notifiers": {
            "console": {"type": "console"},
            "on-call": {"type": "webhook", "url": base + "/webhook"},
        },
        "escalation": [
            {"after_minutes": 0, "notify": ["console"]},
            {"after_minutes": 0.2, "notify": ["on-call"]},
        ],
    }


def seed_history(store: Store, config: Config, now: float, hours: int = 24) -> None:
    """Write plausible past results: mostly fine, two outages, some slowness."""
    rng = random.Random(42)
    step = 60.0  # one probe per minute, so even the 1 h view has no gaps
    start = now - hours * 3600
    # (check, minutes ago it started, minutes it lasted, cause, who acknowledged)
    outages = [
        ("checkout-api", 19 * 60, 34, "HTTP 503 (3 consecutive failures)", "ana"),
        ("checkout-api", 6 * 60 + 20, 12, "timed out (3 consecutive failures)", None),
        ("homepage", 13 * 60, 8, "connection refused (3 consecutive failures)", "marko"),
    ]
    windows = {}
    for name, ago, minutes, cause, who in outages:
        opened = now - ago * 60
        windows.setdefault(name, []).append((opened, opened + minutes * 60, cause))
        incident_id = store.create_incident(name, config.check(name).severity, opened, cause)
        store.set_escalation_level(incident_id, 0, opened, "level 0 -> console")
        if who:
            store.ack_incident(incident_id, opened + rng.randint(60, 240), who)
        store.resolve_incident(incident_id, opened + minutes * 60, "recovered")

    rows = []
    for check in config.checks:
        t = start
        while t < now - step:
            status, latency, detail = "up", rng.uniform(18, 60), "HTTP 200"
            for opened, resolved, cause in windows.get(check.name, []):
                if opened - 3 * step <= t < resolved:
                    status, latency, detail = "down", None, cause.split(" (")[0]
            if status == "up" and check.name == "search" and rng.random() < 0.06:
                status, latency = "degraded", rng.uniform(160, 420)
                detail = "slow: %.0f ms > 150 ms" % latency
            if check.type == "tcp" and status == "up":
                latency, detail = rng.uniform(0.3, 2.0), "port open"
            rows.append((check.name, t, status, latency, detail))
            t += step
    store.add_results(rows)
