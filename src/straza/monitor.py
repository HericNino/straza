"""The run loop: decide which checks are due, run them, feed the results in.

Probes run in a thread pool because they spend their time waiting on the
network, but every result is *processed* on the one loop thread.  That keeps
the incident rules free of race conditions without needing any locking of
their own: two results for the same check can never be handled at once.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Tuple

from . import notify
from .checks import CheckResult, run_check
from .config import CheckConfig, Config
from .incidents import IncidentManager
from .store import Store

log = logging.getLogger("straza.monitor")


class Monitor:
    def __init__(self, config: Config, store: Store,
                 probe: Callable[[CheckConfig], CheckResult] = run_check,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.store = store
        self.probe = probe
        self.clock = clock
        self.incidents = IncidentManager(config, store, notify.build(config))
        # Every check is due immediately on start.
        self._next_run: Dict[str, float] = {c.name: 0.0 for c in config.checks}
        self._pool = ThreadPoolExecutor(max_workers=min(16, max(1, len(config.checks))),
                                        thread_name_prefix="probe")
        self._last_prune = 0.0

    def tick(self) -> List[Tuple[CheckConfig, CheckResult]]:
        """Run every check that is due.  Returns what ran, for callers that
        want to print it (``straza check``) or assert on it (tests)."""
        now = self.clock()
        due = [c for c in self.config.checks if self._next_run[c.name] <= now]
        results = list(self._pool.map(self.probe, due))
        now = self.clock()
        ran = []
        for check, result in zip(due, results):
            # Schedule from "now", not from the previous due time: if the
            # machine was asleep we want one probe, not a burst of catch-up.
            self._next_run[check.name] = now + check.interval
            transition = self.incidents.record(check, result, now)
            if transition:
                log.info("%s: incident %s (%s)", check.name, transition, result.detail)
            ran.append((check, result))
        self.incidents.escalate(now)
        self._prune(now)
        return ran

    def run_forever(self, stop: threading.Event) -> None:
        log.info("monitoring %d checks", len(self.config.checks))
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("tick failed; continuing")
            wait = min(self._next_run.values()) - self.clock()
            # Wake at least once a second so escalation timers and Ctrl-C
            # are never late by more than that.
            stop.wait(max(0.05, min(wait, 1.0)))
        self._pool.shutdown(wait=False)

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        removed = self.store.prune(now - self.config.retention_days * 86400)
        if removed:
            log.info("pruned %d results older than %d days", removed, self.config.retention_days)
