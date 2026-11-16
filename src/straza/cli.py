"""Command line interface.

Exit codes follow the convention scripts and cron expect:
    0  fine          1  something is down / not found          2  bad usage or config
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from typing import List, Optional, Sequence

from . import __version__, config as config_mod, demo as demo_mod, report as report_mod
from .checks import run_check
from .config import Config, ConfigError
from .incidents import format_duration
from .monitor import Monitor
from .status import WINDOWS, snapshot
from .store import Store
from .web import make_server

EXAMPLE = {
    "database": "straza.db",
    "retention_days": 90,
    "web": {"host": "127.0.0.1", "port": 8080, "title": "Service status", "allow_ack": False},
    "defaults": {"interval": 30, "timeout": 5, "failures_to_open": 3, "successes_to_resolve": 2},
    "checks": [
        {"name": "website", "type": "http", "url": "https://example.com/",
         "expect_status": [200], "max_latency_ms": 1500, "severity": "sev1"},
        {"name": "website-cert", "type": "tls", "host": "example.com", "warn_days": 21,
         "interval": 3600, "severity": "sev2"},
        {"name": "dns", "type": "dns", "host": "example.com", "severity": "sev2"},
        {"name": "database-port", "type": "tcp", "host": "127.0.0.1", "port": 5432,
         "severity": "sev1"},
    ],
    "notifiers": {
        "console": {"type": "console"},
        "alert-log": {"type": "file", "path": "alerts.jsonl"},
    },
    "escalation": [
        {"after_minutes": 0, "notify": ["console", "alert-log"]},
        {"after_minutes": 15, "notify": ["console"]},
    ],
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except ConfigError as e:
        print("straza: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="straza", description="Uptime monitoring, incidents "
                                "and escalation in a single SQLite file.")
    p.add_argument("--version", action="version", version="straza " + __version__)
    sub = p.add_subparsers(metavar="command")

    def cmd(name, func, help_text, with_config=True):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        if with_config:
            sp.add_argument("-c", "--config", default="straza.json",
                            help="config file (default: straza.json)")
        return sp

    sp = cmd("init", cmd_init, "write an example config file", with_config=False)
    sp.add_argument("path", nargs="?", default="straza.json")

    sp = cmd("run", cmd_run, "run the monitor and the status page")
    sp.add_argument("--no-web", action="store_true", help="do not start the status page")

    cmd("check", cmd_check, "probe everything once, print results, exit 1 if anything is down")

    sp = cmd("status", cmd_status, "show current state from the database")
    sp.add_argument("--window", choices=list(WINDOWS), default="24h")
    sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = cmd("incidents", cmd_incidents, "list incidents")
    sp.add_argument("--all", action="store_true", help="include resolved incidents")

    sp = cmd("show", cmd_show, "show one incident and its event log")
    sp.add_argument("id", type=int)

    sp = cmd("ack", cmd_ack, "acknowledge an incident (stops escalation)")
    sp.add_argument("id", type=int)
    sp.add_argument("--who", default=None, help="your name (default: current user)")

    sp = cmd("report", cmd_report, "availability report in Markdown")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("-o", "--output", help="write to a file instead of stdout")

    sp = cmd("demo", cmd_demo, "run against a built-in flaky fake service", with_config=False)
    sp.add_argument("--port", type=int, default=8080, help="status page port (default: 8080)")
    sp.add_argument("--no-seed", action="store_true", help="start with an empty history")
    return p


# --------------------------------------------------------------------------
# commands


def cmd_init(args) -> int:
    if os.path.exists(args.path):
        print("straza: %s already exists, not overwriting" % args.path, file=sys.stderr)
        return 2
    with open(args.path, "w", encoding="utf-8") as f:
        json.dump(EXAMPLE, f, indent=2)
        f.write("\n")
    print("wrote %s - edit the checks, then: straza run -c %s" % (args.path, args.path))
    return 0


def cmd_run(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    return _serve(config, store, web=not args.no_web)


def cmd_check(args) -> int:
    config = config_mod.load(args.config)
    rows, worst = [], 0
    for check in config.checks:
        result = run_check(check)
        if not result.ok:
            worst = 1
        rows.append([check.name, result.status.upper(),
                     "%.0f ms" % result.latency_ms if result.latency_ms is not None else "-",
                     result.detail])
    _table(["CHECK", "STATUS", "LATENCY", "DETAIL"], rows)
    return worst


def cmd_status(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    snap = snapshot(config, store, time.time(), args.window)
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0 if snap["overall"] != "down" else 1
    rows = []
    for c in snap["checks"]:
        rows.append([
            c["name"], c["status"].upper(),
            "%.2f%%" % c["uptime_pct"] if c["uptime_pct"] is not None else "-",
            "%.0f ms" % c["p95_latency_ms"] if c["p95_latency_ms"] is not None else "-",
            _ago(c["last_checked"]), c["detail"]])
    _table(["CHECK", "STATUS", "UPTIME " + args.window, "P95", "CHECKED", "DETAIL"], rows)
    if snap["open_incidents"]:
        print("\n%d open incident(s) - see: straza incidents" % len(snap["open_incidents"]))
    return 0 if snap["overall"] != "down" else 1


def cmd_incidents(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    incidents = store.incidents(limit=200) if args.all else list(reversed(store.open_incidents()))
    if not incidents:
        print("no incidents" if args.all else "no open incidents")
        return 0
    now = time.time()
    rows = []
    for i in incidents:
        if i["resolved_at"]:
            state = "resolved"
        elif i["acked_at"]:
            state = "acked by " + (i["acked_by"] or "?")
        else:
            state = "OPEN, unacked"
        rows.append([str(i["id"]), i["severity"].upper(), i["check_name"], state,
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(i["opened_at"])),
                     format_duration((i["resolved_at"] or now) - i["opened_at"]),
                     i["cause"] or ""])
    _table(["ID", "SEV", "CHECK", "STATE", "OPENED", "DURATION", "CAUSE"], rows)
    return 0


def cmd_show(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    incident = store.incident(args.id)
    if incident is None:
        print("straza: no incident #%d" % args.id, file=sys.stderr)
        return 1
    print("Incident #%d  %s  %s" % (incident["id"], incident["severity"].upper(),
                                  incident["check_name"]))
    print("Cause: %s\n" % (incident["cause"] or "-"))
    for e in store.events(args.id):
        print("  %s  %-13s %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e["kind"],
            e["message"] or ""))
    return 0


def cmd_ack(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    who = args.who or getpass.getuser()
    if store.ack_incident(args.id, time.time(), who):
        print("incident #%d acknowledged by %s - escalation stopped" % (args.id, who))
        return 0
    incident = store.incident(args.id)
    if incident is None:
        reason = "no such incident"
    elif incident["resolved_at"]:
        reason = "already resolved"
    else:
        reason = "already acknowledged by %s" % incident["acked_by"]
    print("straza: incident #%d not acknowledged: %s" % (args.id, reason), file=sys.stderr)
    return 1


def cmd_report(args) -> int:
    config = config_mod.load(args.config)
    store = Store(config.database)
    text = report_mod.build(config, store, time.time(), args.days)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote %s" % args.output)
    else:
        sys.stdout.write(text)
    return 0


def cmd_demo(args) -> int:
    target = demo_mod.start_target()
    workdir = tempfile.mkdtemp(prefix="straza-demo-")
    database = os.path.join(workdir, "demo.db")
    config = demo_mod.build_config(target.server_address[1], database, args.port)
    config_path = os.path.join(workdir, "straza.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(demo_mod.raw_config(target.server_address[1], database, args.port), f, indent=2)
    store = Store(config.database)
    if not args.no_seed:
        demo_mod.seed_history(store, config, time.time())
    print("straza demo")
    print("  status page     http://127.0.0.1:%d/   (try ?window=1h)" % args.port)
    print("  other terminal  straza incidents -c %s" % config_path)
    print("  checkout-api goes down for %ds every %ds; nobody is on call, so watch the"
          % (demo_mod.DOWN_SECONDS, demo_mod.UP_SECONDS + demo_mod.DOWN_SECONDS))
    print("  incident escalate to the webhook - or acknowledge it on the page first.")
    print("  Ctrl-C to stop.\n")
    try:
        return _serve(config, store, web=True)
    finally:
        target.shutdown()


# --------------------------------------------------------------------------
# plumbing


def _serve(config: Config, store: Store, web: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    stop = threading.Event()

    def handle(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)  # what `systemctl stop` and `docker stop` send

    server = None
    if web:
        server = make_server(config, store)
        threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
        logging.getLogger("straza").info("status page on http://%s:%d/",
                                         config.web.host, server.server_address[1])
    Monitor(config, store).run_forever(stop)
    if server:
        server.shutdown()
    store.close()
    return 0


def _table(headers: List[str], rows: List[List[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:-1]):
            widths[i] = max(widths[i], len(cell))
    for row in [headers] + rows:
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:-1]))
        print((line + "  " + row[-1]).rstrip())


def _ago(ts: Optional[float]) -> str:
    return "never" if ts is None else format_duration(time.time() - ts) + " ago"
