import pytest

from straza.store import Store


def fill(store, name, statuses, start=0.0, step=60.0, latencies=None):
    for i, status in enumerate(statuses):
        latency = None if status == "down" else (latencies[i] if latencies else 10.0)
        store.add_result(name, start + i * step, status, latency, "")


def test_uptime_is_the_share_of_probes_that_were_not_down(store):
    fill(store, "api", ["up"] * 7 + ["degraded"] + ["down"] * 2)
    s = store.summary(0, 10_000)["api"]
    assert (s["total"], s["down"], s["degraded"]) == (10, 2, 1)
    assert s["uptime_pct"] == pytest.approx(80.0)


def test_summary_only_counts_the_requested_window(store):
    fill(store, "api", ["down"] * 5, start=0)
    fill(store, "api", ["up"] * 5, start=1000)
    assert store.summary(1000, 2000)["api"]["uptime_pct"] == 100.0
    assert "api" not in store.summary(5000, 6000)


@pytest.mark.parametrize("n, expected", [(1, 1), (20, 19), (100, 95), (101, 96)])
def test_p95_is_nearest_rank(store, n, expected):
    # latencies 1..n, inserted in reverse so the query has to sort them
    fill(store, "api", ["up"] * n, latencies=list(range(n, 0, -1)))
    assert store.summary(0, 1e9)["api"]["p95_latency_ms"] == expected


def test_failed_probes_do_not_count_towards_latency(store):
    fill(store, "api", ["up", "down", "down", "up"], latencies=[10, None, None, 30])
    s = store.summary(0, 1e9)["api"]
    assert s["avg_latency_ms"] == 20 and s["p95_latency_ms"] == 30


def test_timeline_buckets(store):
    # 4 buckets of 100 s; probes at 10 (up), 110 (down), 150 (up), 310 (degraded)
    for ts, status in [(10, "up"), (110, "down"), (150, "up"), (310, "degraded")]:
        store.add_result("api", ts, status, 1.0, "")
    series = store.timeline(0, 400, 4)["api"]
    assert [b["total"] for b in series] == [1, 2, 0, 1]
    assert [b["down"] for b in series] == [0, 1, 0, 0]
    assert series[3]["degraded"] == 1


def test_latest_returns_the_newest_row_per_check(store):
    fill(store, "a", ["up", "down"])
    fill(store, "b", ["down", "up"])
    latest = store.latest()
    assert (latest["a"]["status"], latest["b"]["status"]) == ("down", "up")


def test_incident_stats_clip_downtime_to_the_window(store):
    first = store.create_incident("api", "sev1", 100, "x")     # 100..400, acked at 160
    store.ack_incident(first, 160, "nino")
    store.resolve_incident(first, 400, "ok")
    second = store.create_incident("api", "sev1", 900, "y")    # 900..still open
    stats = store.incident_stats(0, 1000)["api"]
    assert stats["incidents"] == 2
    assert stats["downtime_s"] == 300 + 100       # open incident counted up to "until"
    assert stats["mtta_s"] == 60 and stats["mttr_s"] == 300
    # a window that starts mid-incident only gets the part inside it
    assert store.incident_stats(300, 500)["api"]["downtime_s"] == 100
    assert second != first


def test_prune_removes_old_results_but_keeps_incidents(store):
    fill(store, "api", ["up"] * 10)                    # ts 0..540
    store.create_incident("api", "sev1", 0, "x")
    assert store.prune(300) == 5
    assert store.summary(0, 1e9)["api"]["total"] == 5
    assert len(store.incidents()) == 1


def test_a_second_process_can_write_while_the_first_has_the_file_open(tmp_path):
    path = str(tmp_path / "s.db")
    monitor, cli = Store(path), Store(path)
    incident_id = monitor.create_incident("api", "sev1", 1, "x")
    assert cli.ack_incident(incident_id, 2, "nino") is True
    assert monitor.open_incident("api")["acked_by"] == "nino"
    monitor.close(), cli.close()
