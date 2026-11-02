"""Pacing never fabricates limit errors; retries happen only on real 429s."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from viper.fyers.rest import FyersError, FyersRest, NotAuthenticated
from viper.net.ratelimit import BackoffPolicy, SingleFlight, TokenBucket


def test_token_bucket_waits_instead_of_failing():
    b = TokenBucket(rate_per_sec=50, burst=5)
    t0 = time.monotonic()
    for _ in range(15):
        b.acquire()  # must never raise
    assert time.monotonic() - t0 >= 0.15  # 10 extra tokens at 50/s


def test_single_flight_coalesces_concurrent_calls():
    sf = SingleFlight()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.15)
        return 42

    out = []
    ts = [threading.Thread(target=lambda: out.append(sf.do("k", slow))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == [42] * 8
    assert len(calls) == 1
    assert sf.coalesced == 7


def test_backoff_honours_retry_after():
    p = BackoffPolicy()
    assert p.delay(0, "3") == 3.0
    assert p.delay(3) <= p.cap_s * 1.2


class _Scripted(BaseHTTPRequestHandler):
    script: list = []
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        status, body = self.script.pop(0) if self.script else (200, {"s": "ok"})
        raw = json.dumps(body).encode()
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    _Scripted.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Scripted)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/x"
    srv.shutdown()


def _rest():
    r = FyersRest("data", "APP-100", None, rate_per_sec=100, burst=100, max_retries=3)
    r.set_token("APP-100", "tok")
    return r


def test_retries_only_on_real_429(server):
    _Scripted.script = [(429, {"s": "error", "code": 429, "message": "limit"}), (429, {"s": "error"}), (200, {"s": "ok", "d": 1})]
    r = _rest()
    assert r.request("GET", server)["d"] == 1
    assert r.stats.http_429 == 2 and r.stats.retries == 2


def test_other_errors_are_not_retried(server):
    _Scripted.script = [(400, {"s": "error", "code": -50, "message": "bad symbol"})]
    r = _rest()
    with pytest.raises(FyersError) as e:
        r.request("GET", server)
    assert "bad symbol" in str(e.value)
    assert _Scripted.hits == 1 and r.stats.http_429 == 0


def test_expired_token_is_auth_error_not_limit(server):
    _Scripted.script = [(401, {"s": "error", "code": -15, "message": "token expired"})]
    with pytest.raises(NotAuthenticated):
        _rest().request("GET", server)


def test_no_token_raises_auth_error_without_network():
    r = FyersRest("data", "APP-100", None)
    with pytest.raises(NotAuthenticated):
        r.quotes(["NSE:NIFTY50-INDEX"])
