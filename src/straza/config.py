"""Configuration loading and validation.

The config is plain JSON so that straza needs nothing outside the standard
library.  Everything is validated up front: a monitor that starts with a typo
in its config and silently skips a check is worse than one that refuses to
start, so every problem is collected and reported with the path to the
offending key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CHECK_TYPES = ("http", "tcp", "dns", "tls")
SEVERITIES = ("sev1", "sev2", "sev3")
NOTIFIER_TYPES = ("console", "file", "webhook")


class ConfigError(ValueError):
    """Raised when the config is invalid.  ``problems`` lists every issue."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class CheckConfig:
    name: str
    type: str
    severity: str = "sev2"
    interval: float = 30.0
    timeout: float = 5.0
    failures_to_open: int = 3
    successes_to_resolve: int = 2
    # http
    url: Optional[str] = None
    expect_status: tuple = (200,)
    expect_body: Optional[str] = None
    max_latency_ms: Optional[float] = None
    # tcp / dns / tls
    host: Optional[str] = None
    port: Optional[int] = None
    expect_ip: Optional[str] = None
    warn_days: int = 14

    @property
    def target(self) -> str:
        """Human-readable description of what this check probes."""
        if self.type == "http":
            return self.url or ""
        if self.type == "dns":
            return str(self.host)
        return "%s:%s" % (self.host, self.port)


@dataclass(frozen=True)
class NotifierConfig:
    name: str
    type: str
    path: Optional[str] = None
    url: Optional[str] = None


@dataclass(frozen=True)
class EscalationStep:
    after_minutes: float
    notify: tuple


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    title: str = "Service status"
    allow_ack: bool = False


@dataclass(frozen=True)
class Config:
    database: str = "straza.db"
    retention_days: int = 90
    web: WebConfig = field(default_factory=WebConfig)
    checks: tuple = ()
    notifiers: tuple = ()
    escalation: tuple = ()

    def check(self, name: str) -> Optional[CheckConfig]:
        for c in self.checks:
            if c.name == name:
                return c
        return None


def load(path: str) -> Config:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(["config file not found: %s" % path])
    except json.JSONDecodeError as e:
        raise ConfigError(["%s is not valid JSON: %s" % (path, e)])
    cfg = parse(raw)
    # A relative database path is relative to the config file, not to wherever
    # the process happened to be started from (systemd starts it in /).
    db = Path(cfg.database)
    if cfg.database != ":memory:" and not db.is_absolute():
        cfg = _replace(cfg, database=str(p.parent / db))
    return cfg


def _replace(cfg: Config, **changes: Any) -> Config:
    data = {f: getattr(cfg, f) for f in cfg.__dataclass_fields__}
    data.update(changes)
    return Config(**data)


def parse(raw: Any) -> Config:
    """Turn a decoded JSON document into a validated Config."""
    problems: List[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["top level must be a JSON object"])

    _unknown(raw, {"database", "retention_days", "web", "defaults", "checks",
                   "notifiers", "escalation"}, "", problems)

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        problems.append("defaults: must be an object")
        defaults = {}

    web = _parse_web(raw.get("web", {}), problems)
    notifiers = _parse_notifiers(raw.get("notifiers", {}), problems)
    checks = _parse_checks(raw.get("checks", []), defaults, problems)
    escalation = _parse_escalation(raw.get("escalation", []),
                                   {n.name for n in notifiers}, problems)

    retention = raw.get("retention_days", 90)
    if not _is_num(retention) or retention <= 0:
        problems.append("retention_days: must be a positive number")
        retention = 90

    database = raw.get("database", "straza.db")
    if not isinstance(database, str) or not database:
        problems.append("database: must be a file path")
        database = "straza.db"

    if problems:
        raise ConfigError(problems)
    return Config(database=database, retention_days=int(retention), web=web,
                  checks=tuple(checks), notifiers=tuple(notifiers),
                  escalation=tuple(escalation))


# --------------------------------------------------------------------------
# section parsers


def _parse_web(raw: Any, problems: List[str]) -> WebConfig:
    if not isinstance(raw, dict):
        problems.append("web: must be an object")
        return WebConfig()
    _unknown(raw, {"host", "port", "title", "allow_ack"}, "web.", problems)
    port = raw.get("port", 8080)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        problems.append("web.port: must be an integer between 0 and 65535")
        port = 8080
    return WebConfig(host=str(raw.get("host", "127.0.0.1")), port=port,
                     title=str(raw.get("title", "Service status")),
                     allow_ack=bool(raw.get("allow_ack", False)))


def _parse_notifiers(raw: Any, problems: List[str]) -> List[NotifierConfig]:
    out: List[NotifierConfig] = []
    if not isinstance(raw, dict):
        problems.append("notifiers: must be an object mapping name -> settings")
        return out
    for name, spec in raw.items():
        where = "notifiers.%s" % name
        if not isinstance(spec, dict):
            problems.append("%s: must be an object" % where)
            continue
        kind = spec.get("type")
        if kind not in NOTIFIER_TYPES:
            problems.append("%s.type: must be one of %s" % (where, ", ".join(NOTIFIER_TYPES)))
            continue
        _unknown(spec, {"type", "path", "url"}, where + ".", problems)
        if kind == "file" and not isinstance(spec.get("path"), str):
            problems.append("%s.path: required for a file notifier" % where)
        if kind == "webhook" and not _is_http_url(spec.get("url")):
            problems.append("%s.url: required for a webhook notifier (http/https URL)" % where)
        out.append(NotifierConfig(name=name, type=kind, path=spec.get("path"),
                                  url=spec.get("url")))
    return out


_CHECK_KEYS = {"name", "type", "severity", "interval", "timeout", "failures_to_open",
               "successes_to_resolve", "url", "expect_status", "expect_body",
               "max_latency_ms", "host", "port", "expect_ip", "warn_days"}
_DEFAULT_KEYS = {"severity", "interval", "timeout", "failures_to_open",
                 "successes_to_resolve"}


def _parse_checks(raw: Any, defaults: Dict[str, Any], problems: List[str]) -> List[CheckConfig]:
    out: List[CheckConfig] = []
    _unknown(defaults, _DEFAULT_KEYS, "defaults.", problems)
    if not isinstance(raw, list) or not raw:
        problems.append("checks: must be a non-empty list")
        return out
    seen = set()
    for i, spec in enumerate(raw):
        where = "checks[%d]" % i
        if not isinstance(spec, dict):
            problems.append("%s: must be an object" % where)
            continue
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append("%s.name: required" % where)
            continue
        where = "checks[%s]" % name
        if name in seen:
            problems.append("%s: duplicate check name" % where)
            continue
        seen.add(name)
        _unknown(spec, _CHECK_KEYS, where + ".", problems)

        merged = dict(defaults)
        merged.update(spec)
        kind = merged.get("type")
        if kind not in CHECK_TYPES:
            problems.append("%s.type: must be one of %s" % (where, ", ".join(CHECK_TYPES)))
            continue

        before = len(problems)
        if merged.get("severity", "sev2") not in SEVERITIES:
            problems.append("%s.severity: must be one of %s" % (where, ", ".join(SEVERITIES)))
        for key in ("interval", "timeout"):
            if key in merged and (not _is_num(merged[key]) or merged[key] <= 0):
                problems.append("%s.%s: must be a positive number of seconds" % (where, key))
        for key in ("failures_to_open", "successes_to_resolve"):
            v = merged.get(key, 1)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                problems.append("%s.%s: must be an integer >= 1" % (where, key))

        if kind == "http":
            if not _is_http_url(merged.get("url")):
                problems.append("%s.url: required for an http check (http/https URL)" % where)
            status = merged.get("expect_status", [200])
            if isinstance(status, int):
                status = [status]
            if (not isinstance(status, list) or not status
                    or not all(isinstance(s, int) and 100 <= s <= 599 for s in status)):
                problems.append("%s.expect_status: must be a status code or list of them" % where)
            else:
                merged["expect_status"] = tuple(status)
            lat = merged.get("max_latency_ms")
            if lat is not None and (not _is_num(lat) or lat <= 0):
                problems.append("%s.max_latency_ms: must be a positive number" % where)
        else:
            if not isinstance(merged.get("host"), str) or not merged.get("host"):
                problems.append("%s.host: required for a %s check" % (where, kind))
            if kind == "tls":
                merged.setdefault("port", 443)
            if kind in ("tcp", "tls"):
                port = merged.get("port")
                if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                    problems.append("%s.port: must be an integer between 1 and 65535" % where)

        if len(problems) > before:
            continue
        fields = {k: v for k, v in merged.items() if k in _CHECK_KEYS}
        for key in ("interval", "timeout"):
            if key in fields:
                fields[key] = float(fields[key])
        out.append(CheckConfig(**fields))
    return out


def _parse_escalation(raw: Any, notifier_names: set, problems: List[str]) -> List[EscalationStep]:
    out: List[EscalationStep] = []
    if not isinstance(raw, list):
        problems.append("escalation: must be a list of steps")
        return out
    for i, step in enumerate(raw):
        where = "escalation[%d]" % i
        if not isinstance(step, dict):
            problems.append("%s: must be an object" % where)
            continue
        _unknown(step, {"after_minutes", "notify"}, where + ".", problems)
        after = step.get("after_minutes", 0)
        if not _is_num(after) or after < 0:
            problems.append("%s.after_minutes: must be a number >= 0" % where)
            continue
        notify = step.get("notify", [])
        if not isinstance(notify, list) or not notify:
            problems.append("%s.notify: must be a non-empty list of notifier names" % where)
            continue
        missing = [n for n in notify if n not in notifier_names]
        if missing:
            problems.append("%s.notify: unknown notifier(s): %s" % (where, ", ".join(map(str, missing))))
            continue
        out.append(EscalationStep(after_minutes=float(after), notify=tuple(notify)))
    # Steps are applied in order of delay, whatever order they were written in.
    out.sort(key=lambda s: s.after_minutes)
    return out


# --------------------------------------------------------------------------
# helpers


def _unknown(obj: Dict[str, Any], allowed: set, prefix: str, problems: List[str]) -> None:
    for key in obj:
        if key not in allowed:
            problems.append("%s%s: unknown key" % (prefix, key))


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_http_url(v: Any) -> bool:
    return isinstance(v, str) and (v.startswith("http://") or v.startswith("https://"))
