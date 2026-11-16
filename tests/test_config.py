import json

import pytest

from straza import config as config_mod
from straza.config import ConfigError

from conftest import make_config


def test_minimal_config_gets_defaults():
    cfg = config_mod.parse({"checks": [{"name": "web", "type": "http", "url": "https://x.test/"}]})
    check = cfg.checks[0]
    assert (check.interval, check.timeout, check.severity) == (30.0, 5.0, "sev2")
    assert check.expect_status == (200,)
    assert cfg.web.port == 8080 and cfg.web.allow_ack is False


def test_defaults_section_applies_and_check_overrides_it():
    cfg = config_mod.parse({
        "defaults": {"interval": 10, "severity": "sev3"},
        "checks": [{"name": "a", "type": "tcp", "host": "h", "port": 22},
                   {"name": "b", "type": "tcp", "host": "h", "port": 22, "interval": 99}]})
    assert [c.interval for c in cfg.checks] == [10.0, 99.0]
    assert cfg.checks[0].severity == "sev3"


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as err:
        config_mod.parse({
            "checks": [{"name": "a", "type": "http"},                 # no url
                       {"name": "a", "type": "tcp", "host": "h", "port": 1},  # duplicate
                       {"name": "c", "type": "ping", "host": "h"},    # unknown type
                       {"name": "d", "type": "tcp", "host": "h", "port": 70000}],
            "escalation": [{"after_minutes": 0, "notify": ["nobody"]}],
            "typo": 1})
    text = "\n".join(err.value.problems)
    for expected in ("checks[a].url", "duplicate", "checks[c].type", "checks[d].port",
                     "unknown notifier", "typo: unknown key"):
        assert expected in text


def test_unknown_check_key_is_rejected():
    # "intervall" would otherwise be ignored and the check would silently run
    # at the default interval.
    with pytest.raises(ConfigError) as err:
        config_mod.parse({"checks": [{"name": "a", "type": "dns", "host": "h", "intervall": 5}]})
    assert "intervall" in str(err.value)


def test_escalation_steps_are_sorted_by_delay():
    cfg = make_config(escalation=[{"after_minutes": 10, "notify": ["second"]},
                                  {"after_minutes": 0, "notify": ["first"]}])
    assert [s.after_minutes for s in cfg.escalation] == [0.0, 10.0]


def test_tls_check_defaults_to_port_443():
    cfg = config_mod.parse({"checks": [{"name": "cert", "type": "tls", "host": "x.test"}]})
    assert cfg.checks[0].port == 443 and cfg.checks[0].target == "x.test:443"


def test_load_resolves_database_next_to_the_config_file(tmp_path):
    path = tmp_path / "straza.json"
    path.write_text(json.dumps({"database": "data.db",
                                "checks": [{"name": "a", "type": "dns", "host": "h"}]}))
    assert config_mod.load(str(path)).database == str(tmp_path / "data.db")


def test_load_reports_missing_file_and_bad_json(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        config_mod.load(str(tmp_path / "nope.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        config_mod.load(str(bad))
