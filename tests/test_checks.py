import socket

from straza.checks import DEGRADED, DOWN, UP, run_check
from straza.config import CheckConfig


def http(url, **kw):
    return CheckConfig(name="t", type="http", url=url, timeout=2, **kw)


def test_http_up(target):
    result = run_check(http(target.url))
    assert result.status == UP and result.detail == "HTTP 200"
    assert result.latency_ms is not None and result.latency_ms >= 0


def test_http_wrong_status_is_down_with_the_code_in_the_detail(target):
    target.status = 503
    result = run_check(http(target.url))
    assert (result.status, result.detail, result.latency_ms) == (DOWN, "HTTP 503", None)


def test_http_can_expect_a_non_200_status(target):
    target.status = 401
    assert run_check(http(target.url, expect_status=(401,))).status == UP


def test_http_body_marker(target):
    target.body = "maintenance page"
    result = run_check(http(target.url, expect_body="ok"))
    assert result.status == DOWN and "missing 'ok'" in result.detail
    target.body = '{"status": "ok"}'
    assert run_check(http(target.url, expect_body="ok")).status == UP


def test_slow_but_correct_answer_is_degraded_not_down(target):
    target.delay = 0.08
    result = run_check(http(target.url, max_latency_ms=20))
    assert result.status == DEGRADED and result.ok
    assert result.detail.startswith("slow:")


def test_http_timeout(target):
    target.delay = 1.0
    result = run_check(CheckConfig(name="t", type="http", url=target.url, timeout=0.2))
    assert (result.status, result.detail) == (DOWN, "timed out")


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # free now, and nobody is listening on it


def test_connection_refused_is_reported_in_plain_words():
    port = _closed_port()
    result = run_check(http("http://127.0.0.1:%d/" % port))
    assert (result.status, result.detail) == (DOWN, "connection refused")
    result = run_check(CheckConfig(name="t", type="tcp", host="127.0.0.1", port=port, timeout=2))
    assert (result.status, result.detail) == (DOWN, "connection refused")


def test_tcp_up(target):
    result = run_check(CheckConfig(name="t", type="tcp", host="127.0.0.1", port=target.port))
    assert result.status == UP


def test_dns():
    assert run_check(CheckConfig(name="t", type="dns", host="localhost")).status == UP
    wrong = run_check(CheckConfig(name="t", type="dns", host="localhost", expect_ip="10.9.9.9"))
    assert wrong.status == DOWN and "expected 10.9.9.9" in wrong.detail
    missing = run_check(CheckConfig(name="t", type="dns", host="no-such-host.invalid"))
    assert (missing.status, missing.detail) == (DOWN, "DNS lookup failed")
