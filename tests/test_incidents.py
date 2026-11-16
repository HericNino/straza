"""The incident rules, driven with a hand-set clock - no sleeping, no network."""
from straza.checks import CheckResult
from straza.incidents import IncidentManager, format_duration

from conftest import Recorder, make_config

FAIL = CheckResult("down", None, "HTTP 503")
OK = CheckResult("up", 12.0, "HTTP 200")
SLOW = CheckResult("degraded", 900.0, "slow")


def setup(store, **overrides):
    config = make_config(**overrides)
    first, second = Recorder(), Recorder()
    manager = IncidentManager(config, store, {"first": first, "second": second})
    return manager, config.checks[0], first, second


def feed(manager, check, results, start=0.0, step=30.0):
    """Record a sequence of results 30 s apart; return the transitions."""
    return [manager.record(check, r, start + i * step) for i, r in enumerate(results)]


def test_single_failure_does_not_open_an_incident(store):
    manager, check, first, _ = setup(store)
    assert feed(manager, check, [OK, FAIL, OK, FAIL, FAIL, OK]) == [None] * 6
    assert store.open_incidents() == [] and first.sent == []


def test_incident_opens_on_the_third_consecutive_failure(store):
    manager, check, first, second = setup(store)
    assert feed(manager, check, [FAIL, FAIL, FAIL]) == [None, None, "opened"]
    incident = store.open_incident("api")
    assert incident["severity"] == "sev1"
    assert incident["cause"] == "HTTP 503 (3 consecutive failures)"
    assert first.kinds() == ["opened"] and second.sent == []


def test_failures_during_an_open_incident_do_not_open_another(store):
    manager, check, first, _ = setup(store)
    feed(manager, check, [FAIL] * 8)
    assert len(store.incidents()) == 1 and first.kinds() == ["opened"]


def test_flapping_does_not_resolve(store):
    manager, check, _, _ = setup(store)
    transitions = feed(manager, check, [FAIL, FAIL, FAIL, OK, FAIL, OK, FAIL])
    assert transitions[3:] == [None, None, None, None]
    assert store.open_incident("api") is not None


def test_resolves_after_two_consecutive_successes_and_tells_whoever_was_told(store):
    manager, check, first, second = setup(store)
    transitions = feed(manager, check, [FAIL, FAIL, FAIL, OK, OK])
    assert transitions[-1] == "resolved"
    assert store.open_incident("api") is None
    assert first.kinds() == ["opened", "resolved"]
    assert second.sent == []  # never escalated to, so never bothered
    assert first.sent[-1][2] == "down for 1m 00s"   # opened at t=60, resolved at t=120


def test_degraded_counts_as_working_for_incident_purposes(store):
    manager, check, _, _ = setup(store)
    assert feed(manager, check, [SLOW, SLOW, SLOW, SLOW]) == [None] * 4
    assert feed(manager, check, [FAIL, FAIL, FAIL, SLOW, SLOW], start=1000)[-1] == "resolved"


def test_unacknowledged_incident_escalates_once_when_its_delay_passes(store):
    manager, check, first, second = setup(store)
    feed(manager, check, [FAIL, FAIL, FAIL])           # opened at t=60
    assert manager.escalate(60 + 9 * 60) == []         # 9 min: not yet
    assert manager.escalate(60 + 10 * 60) != []        # 10 min: level 1
    assert manager.escalate(60 + 30 * 60) == []        # no further steps, no repeats
    assert second.kinds() == ["escalated"]
    assert second.sent[0][2] == "unacknowledged for 10m 00s"
    assert store.open_incident("api")["escalation_level"] == 1


def test_acknowledging_stops_escalation_but_not_the_incident(store):
    manager, check, first, second = setup(store)
    feed(manager, check, [FAIL, FAIL, FAIL])
    incident_id = store.open_incident("api")["id"]
    assert manager.acknowledge(incident_id, "nino", 120.0) is True
    assert manager.acknowledge(incident_id, "someone-else", 130.0) is False  # first ack wins
    assert manager.escalate(60 + 60 * 60) == [] and second.sent == []
    assert store.open_incident("api")["acked_by"] == "nino"


def test_resolution_reaches_every_level_that_was_notified(store):
    manager, check, first, second = setup(store)
    feed(manager, check, [FAIL, FAIL, FAIL])
    manager.escalate(60 + 11 * 60)
    feed(manager, check, [OK, OK], start=2000)
    assert first.kinds() == ["opened", "resolved"]
    assert second.kinds() == ["escalated", "resolved"]


def test_steps_missed_while_the_monitor_was_down_all_fire(store):
    # Incident opens, monitor is restarted and comes back 20 minutes later:
    # the 10-minute step must not be skipped.
    manager, check, first, second = setup(store, escalation=[
        {"after_minutes": 5, "notify": ["first"]}, {"after_minutes": 10, "notify": ["second"]}])
    feed(manager, check, [FAIL, FAIL, FAIL])
    assert first.sent == []                            # nothing at minute 0 in this ladder
    manager.escalate(60 + 20 * 60)
    assert first.kinds() == ["opened"] and second.kinds() == ["escalated"]


def test_a_failing_notifier_does_not_break_incident_handling(store):
    manager, check, first, _ = setup(store)

    def boom(*args, **kwargs):
        raise OSError("webhook unreachable")

    first.send = boom
    assert feed(manager, check, [FAIL, FAIL, FAIL, OK, OK])[-1] == "resolved"


def test_event_log_tells_the_story_in_order(store):
    manager, check, _, _ = setup(store)
    feed(manager, check, [FAIL, FAIL, FAIL])
    incident_id = store.open_incident("api")["id"]
    manager.acknowledge(incident_id, "nino", 100.0)
    feed(manager, check, [OK, OK], start=200)
    assert [e["kind"] for e in store.events(incident_id)] == [
        "opened", "escalated", "acknowledged", "resolved"]


def test_format_duration():
    assert [format_duration(s) for s in (5, 65, 3600, 90000, 200000)] == [
        "5s", "1m 05s", "1h 00m", "25h 00m", "2d 07h"]
