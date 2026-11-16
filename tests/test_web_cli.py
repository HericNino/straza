import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from straza import cli, config as config_mod, demo, report
from straza.checks import CheckResult
from straza.monitor import Monitor
from straza.status import snapshot
from straza.store import Store
from straza.web import make_server

from conftest import make_config


@pytest.fixture
def site(store):
    """A status page on a free port, over a store with one open incident."""
    config = make_config(web={"port": 0, "allow_ack": True, "title": "Test <status>"})
    for ts in (10, 20, 30):
        store.add_result("api", ts, "down", None, "<b>HTTP 503</b>")
    store.create_incident("api", "sev1", 30, "<b>HTTP 503</b>")
    server = make_server(config, store, clock=lambda: 100.0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_address[1], store
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def test_status_page_shows_the_incident_and_escapes_untrusted_text(site):
    base, _ = site
    code, html = get(base + "/")
    assert code == 200
    assert "There is an ongoing incident" in html and "#1 api" in html
    assert "<b>HTTP 503</b>" not in html and "&lt;b&gt;HTTP 503&lt;/b&gt;" in html
    assert "<title>Test &lt;status&gt;</title>" in html


def test_api_status_matches_the_snapshot(site):
    base, _ = site
    data = json.loads(get(base + "/api/status?window=1h")[1])
    assert data["overall"] == "down" and data["window"] == "1h"
    assert data["checks"][0]["uptime_pct"] == 0.0
    assert len(data["checks"][0]["timeline"]) == 60


def test_acknowledge_from_the_page(site):
    base, store = site
    body = urllib.parse.urlencode({"who": "nino"}).encode()
    with urllib.request.urlopen(urllib.request.Request(base + "/incidents/1/ack", data=body)) as r:
        assert r.status == 200  # followed the 303 back to the page
        assert "Acknowledged by nino" in r.read().decode()
    assert store.incident(1)["acked_by"] == "nino"
    events = json.loads(get(base + "/api/incidents/1")[1])["events"]
    assert [e["kind"] for e in events] == ["opened", "acknowledged"]


def test_acknowledge_is_refused_unless_enabled(store):
    server = make_server(make_config(web={"port": 0}), store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/incidents/1/ack" % server.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(url, data=b"who=x"))
    assert err.value.code == 403
    server.shutdown()
    server.server_close()


def test_healthz_and_404(site):
    base, _ = site
    assert get(base + "/healthz") == (200, "ok\n")
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "/nope")
    assert err.value.code == 404


def test_monitor_tick_runs_due_checks_and_respects_intervals(store):
    now = [1000.0]
    calls = []

    def probe(check):
        calls.append(check.name)
        return CheckResult("down", None, "HTTP 500")

    config = make_config(checks=[
        {"name": "fast", "type": "dns", "host": "h", "interval": 10, "failures_to_open": 2},
        {"name": "slow", "type": "dns", "host": "h", "interval": 60}])
    monitor = Monitor(config, store, probe=probe, clock=lambda: now[0])
    monitor.tick()
    assert sorted(calls) == ["fast", "slow"]
    now[0] += 5
    monitor.tick()                        # nothing due yet
    assert len(calls) == 2
    now[0] += 5
    monitor.tick()                        # only the 10 s check is due
    assert calls[2:] == ["fast"]
    assert store.open_incident("fast") is not None   # 2 failures in a row
    assert store.open_incident("slow") is None
    assert snapshot(config, store, now[0])["overall"] == "down"


def test_report_contains_uptime_and_incident_table(store):
    config = make_config()
    now = 10 * 86400.0
    for i in range(100):
        store.add_result("api", now - 1000 + i, "down" if i < 5 else "up", 20.0 if i >= 5 else None, "")
    incident = store.create_incident("api", "sev1", now - 1000, "HTTP 503 | bad gateway")
    store.ack_incident(incident, now - 940, "nino")
    store.resolve_incident(incident, now - 700, "ok")
    text = report.build(config, store, now, days=7)
    assert "| api | sev1 | 95.000% | 100 | 1 | 5m 00s | 1m 00s | 5m 00s | 20 ms |" in text
    assert "HTTP 503 / bad gateway" in text      # a pipe would break the Markdown table


def test_cli_init_check_and_ack(tmp_path, target, capsys):
    path = str(tmp_path / "straza.json")
    assert cli.main(["init", path]) == 0
    assert cli.main(["init", path]) == 2          # refuses to overwrite
    config_mod.load(path)                         # the example it writes is valid

    cfg = tmp_path / "live.json"
    cfg.write_text(json.dumps({"checks": [
        {"name": "web", "type": "http", "url": target.url}]}))
    assert cli.main(["check", "-c", str(cfg)]) == 0
    target.status = 500
    assert cli.main(["check", "-c", str(cfg)]) == 1
    assert "HTTP 500" in capsys.readouterr().out

    db = Store(str(tmp_path / "straza.db"))
    incident = db.create_incident("web", "sev2", 1, "HTTP 500")
    db.close()
    assert cli.main(["ack", str(incident), "--who", "nino", "-c", str(cfg)]) == 0
    assert cli.main(["ack", str(incident), "-c", str(cfg)]) == 1    # already acknowledged
    assert cli.main(["incidents", "-c", str(cfg)]) == 0
    assert "acked by nino" in capsys.readouterr().out
    assert cli.main(["show", "99", "-c", str(cfg)]) == 1
    assert cli.main(["status", "-c", str(tmp_path / "missing.json")]) == 2


def test_demo_config_and_seed_are_consistent(store):
    config = demo.build_config(12345, ":memory:", 8080)
    demo.seed_history(store, config, now=1_000_000.0)
    summary = store.summary(0, 1_000_000.0)
    assert set(summary) == {c.name for c in config.checks}
    assert summary["app-port"]["uptime_pct"] == 100.0
    assert 90 < summary["checkout-api"]["uptime_pct"] < 100
    assert store.open_incidents() == [] and len(store.incidents()) == 3
