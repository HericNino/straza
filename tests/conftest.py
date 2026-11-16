"""Shared fixtures: a local HTTP server whose behaviour each test controls."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from straza import config as config_mod
from straza.store import Store


class Target:
    """A tiny web server.  Tests set .status / .body / .delay to script it."""

    def __init__(self):
        self.status, self.body, self.delay = 200, "ok", 0.0
        target = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                time.sleep(target.delay)
                data = target.body.encode()
                self.send_response(target.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d/" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


class Recorder:
    """Stands in for a real notifier and remembers what it was asked to send."""

    def __init__(self):
        self.sent = []

    def send(self, kind, incident, note=""):
        self.sent.append((kind, incident["id"], note))

    def kinds(self):
        return [k for k, _, _ in self.sent]


def make_config(**overrides):
    raw = {
        "database": ":memory:",
        "checks": [{"name": "api", "type": "http", "url": "http://127.0.0.1:1/",
                    "failures_to_open": 3, "successes_to_resolve": 2, "severity": "sev1"}],
        "notifiers": {"first": {"type": "console"}, "second": {"type": "console"}},
        "escalation": [{"after_minutes": 0, "notify": ["first"]},
                       {"after_minutes": 10, "notify": ["second"]}],
    }
    raw.update(overrides)
    return config_mod.parse(raw)
