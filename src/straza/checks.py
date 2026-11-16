"""Probes: one function per check type, each returning a CheckResult.

A probe never raises.  Whatever goes wrong on the network is the very thing
we are here to observe, so it is turned into a ``down`` result with a detail
string a human can act on ("connection refused", "HTTP 503", "timed out").

Three states, not two:

* ``up``        the target answered correctly
* ``degraded``  it answered correctly but too slowly (or a certificate is
                close to expiry) - worth seeing, not worth paging anyone
* ``down``      it did not answer correctly
"""
from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .config import CheckConfig

UP, DEGRADED, DOWN = "up", "degraded", "down"
_BODY_LIMIT = 64 * 1024  # enough to find a health marker, never a whole download


@dataclass(frozen=True)
class CheckResult:
    status: str
    latency_ms: Optional[float]
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != DOWN


def run_check(check: CheckConfig) -> CheckResult:
    """Run one probe, timing it, and never let an exception escape."""
    probe = _PROBES[check.type]
    start = time.perf_counter()
    try:
        status, detail = probe(check)
    except Exception as e:  # noqa: BLE001 - deliberately broad, see module docstring
        status, detail = DOWN, _describe(e)
    latency = (time.perf_counter() - start) * 1000.0
    if status == DOWN:
        # Latency of a failure is how long it took to fail, which would
        # poison the latency percentiles, so it is not recorded.
        return CheckResult(DOWN, None, detail)
    if status == UP and check.max_latency_ms and latency > check.max_latency_ms:
        status = DEGRADED
        detail = "slow: %.0f ms > %.0f ms" % (latency, check.max_latency_ms)
    return CheckResult(status, round(latency, 1), detail)


# --------------------------------------------------------------------------
# probes - each returns (status, detail) or raises


def _http(check: CheckConfig):
    req = urllib.request.Request(check.url, headers={"User-Agent": "straza/1"})
    try:
        with urllib.request.urlopen(req, timeout=check.timeout) as resp:
            code = resp.status
            body = resp.read(_BODY_LIMIT) if check.expect_body else b""
    except urllib.error.HTTPError as e:
        # A 4xx/5xx is still an answer; whether it is acceptable is the
        # config's decision (a check may legitimately expect a 401).
        code = e.code
        body = e.read(_BODY_LIMIT) if check.expect_body else b""
    if code not in check.expect_status:
        return DOWN, "HTTP %d" % code
    if check.expect_body and check.expect_body not in body.decode("utf-8", "replace"):
        return DOWN, "HTTP %d but body is missing %r" % (code, check.expect_body)
    return UP, "HTTP %d" % code


def _tcp(check: CheckConfig):
    with socket.create_connection((check.host, check.port), timeout=check.timeout):
        return UP, "port %d open" % check.port


def _dns(check: CheckConfig):
    # getaddrinfo has no timeout parameter; it is bounded by the resolver's
    # own settings, which is good enough for a health probe.
    infos = socket.getaddrinfo(check.host, None)
    addresses = sorted({info[4][0] for info in infos})
    if check.expect_ip and check.expect_ip not in addresses:
        return DOWN, "resolved to %s, expected %s" % (", ".join(addresses), check.expect_ip)
    return UP, "resolves to %s" % ", ".join(addresses[:3])


def _tls(check: CheckConfig):
    context = ssl.create_default_context()
    with socket.create_connection((check.host, check.port), timeout=check.timeout) as sock:
        with context.wrap_socket(sock, server_hostname=check.host) as tls:
            cert = tls.getpeercert()
    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    days_left = (expires - time.time()) / 86400.0
    if days_left < 0:
        return DOWN, "certificate expired %.0f days ago" % -days_left
    if days_left < check.warn_days:
        return DEGRADED, "certificate expires in %.0f days" % days_left
    return UP, "certificate valid for %.0f more days" % days_left


_PROBES: Dict[str, Callable] = {"http": _http, "tcp": _tcp, "dns": _dns, "tls": _tls}


def _describe(e: Exception) -> str:
    """Reduce an exception to the short phrase an on-call person wants."""
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, Exception):
        e = e.reason
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(e, ssl.SSLCertVerificationError):
        return "TLS certificate rejected: %s" % (e.verify_message or e.reason)
    if isinstance(e, ssl.SSLError):
        return "TLS error: %s" % (e.reason or e)
    text = str(e).strip() or type(e).__name__
    return text[:200]
