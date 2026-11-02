"""Thin FYERS API v3 REST client, one instance per app role.

Why not ``fyers_apiv3.fyersModel.FyersModel``? It creates its own unbound
``requests.Session`` and writes log files into the working directory. This client
uses the same endpoints (``https://api-t1.fyers.in/api/v3`` and ``/data``), the
same ``Authorization: <app_id>:<access_token>`` header, and adds:

* a source-IP bound session (``SourceAddressAdapter``),
* a pacing token bucket that waits rather than failing,
* single-flight coalescing plus a short TTL cache for idempotent market reads,
* retry with exponential backoff **only** on a genuine HTTP 429 / rate-limit body.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

from viper.net.binding import bound_to, make_session
from viper.net.ratelimit import BackoffPolicy, SingleFlight, TokenBucket, TTLCache

log = logging.getLogger("viper.fyers.rest")

API = "https://api-t1.fyers.in/api/v3"
DATA = "https://api-t1.fyers.in/data"

# Fyers reports throttling either as HTTP 429 or code 429 in the JSON body.
_RATE_LIMIT_CODES = {429, -429}


class FyersError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class NotAuthenticated(FyersError):
    pass


@dataclass
class RestStats:
    requests: int = 0
    http_429: int = 0
    retries: int = 0
    cache_hits: int = 0
    last_error: str = ""
    last_ok_ts: float = 0.0
    latency_ms: list[float] = field(default_factory=list)

    def snapshot(self, bucket: TokenBucket, sf: SingleFlight) -> dict:
        lat = self.latency_ms[-50:]
        return {
            "requests": self.requests,
            "http_429": self.http_429,
            "retries": self.retries,
            "cache_hits": self.cache_hits,
            "coalesced": sf.coalesced,
            "pacing_wait_ms": round(bucket.waited_ms, 1),
            "p50_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
            "last_error": self.last_error,
            "last_ok_ts": self.last_ok_ts,
        }


class FyersRest:
    def __init__(
        self,
        role: str,
        app_id: str,
        source_ip: str | None,
        rate_per_sec: float = 8.0,
        burst: int = 10,
        max_retries: int = 5,
        quote_cache_ms: int = 750,
        timeout: float = 10.0,
    ):
        self.role = role
        self.app_id = app_id
        self.source_ip = source_ip
        self.access_token = ""
        self.session: requests.Session = make_session(source_ip)
        self.bucket = TokenBucket(rate_per_sec, burst)
        self.sf = SingleFlight()
        self.cache = TTLCache()
        self.backoff = BackoffPolicy(max_retries=max_retries)
        self.quote_ttl = quote_cache_ms / 1000
        self.timeout = timeout
        self.stats = RestStats()

    # ------------------------------------------------------------------
    def set_token(self, app_id: str, access_token: str) -> None:
        self.app_id = app_id
        self.access_token = access_token
        self.cache = TTLCache()

    @property
    def authed(self) -> bool:
        return bool(self.app_id and self.access_token)

    def _headers(self) -> dict:
        if not self.authed:
            raise NotAuthenticated(f"{self.role} app has no access token; reconnect from the Market page", code=-16)
        return {"Authorization": f"{self.app_id}:{self.access_token}", "Content-Type": "application/json", "version": "3"}

    def request(self, method: str, url: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        headers = self._headers()
        attempt = 0
        while True:
            self.bucket.acquire()
            t0 = time.monotonic()
            self.stats.requests += 1
            try:
                # bound_to() covers any connect not made through our adapter (redirects, proxies).
                with bound_to(self.source_ip):
                    resp = self.session.request(
                        method, url, params=params,
                        data=json.dumps(body) if body is not None else None,
                        headers=headers, timeout=self.timeout,
                    )
            except requests.RequestException as e:
                self.stats.last_error = f"network: {e}"
                if attempt < 2 and method == "GET":
                    attempt += 1
                    time.sleep(0.3 * attempt)
                    continue
                raise FyersError(f"network error calling Fyers ({self.role}): {e}") from e
            self.stats.latency_ms.append((time.monotonic() - t0) * 1000)
            if len(self.stats.latency_ms) > 500:
                del self.stats.latency_ms[:250]
            try:
                payload = resp.json()
            except ValueError:
                payload = {"s": "error", "message": resp.text[:300]}
            code = payload.get("code") if isinstance(payload, dict) else None
            throttled = resp.status_code == 429 or code in _RATE_LIMIT_CODES
            if throttled:
                self.stats.http_429 += 1
                if attempt >= self.backoff.max_retries:
                    self.stats.last_error = "HTTP 429 from Fyers after retries"
                    raise FyersError("Fyers returned HTTP 429 (rate limited) after retries", code=429, payload=payload)
                delay = self.backoff.delay(attempt, resp.headers.get("Retry-After"))
                log.warning("%s %s throttled by Fyers (429); retry %d in %.2fs", method, url, attempt + 1, delay)
                self.stats.retries += 1
                attempt += 1
                time.sleep(delay)
                continue
            if resp.status_code == 401 or code in (-8, -15, -16, -17):
                self.stats.last_error = payload.get("message", "token invalid/expired")
                raise NotAuthenticated(self.stats.last_error, code=code, payload=payload)
            if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("s") == "error"):
                msg = payload.get("message", f"HTTP {resp.status_code}") if isinstance(payload, dict) else str(payload)
                self.stats.last_error = msg
                raise FyersError(msg, code=code, payload=payload)
            self.stats.last_ok_ts = time.time()
            return payload

    def _cached_get(self, url: str, params: dict, ttl: float) -> dict:
        key = (url, tuple(sorted(params.items())))
        hit = self.cache.get(key)
        if hit is not None:
            self.stats.cache_hits += 1
            return hit

        def run() -> dict:
            out = self.request("GET", url, params=params)
            if ttl > 0:
                self.cache.put(key, out, ttl)
            return out

        return self.sf.do(key, run)

    # ---- account / trading (trading role) -------------------------------
    def profile(self) -> dict:
        return self.request("GET", f"{API}/profile")

    def funds(self) -> dict:
        return self._cached_get(f"{API}/funds", {}, 2.0)

    def positions(self) -> dict:
        return self.request("GET", f"{API}/positions")

    def orderbook(self) -> dict:
        return self.request("GET", f"{API}/orders")

    def tradebook(self) -> dict:
        return self.request("GET", f"{API}/tradebook")

    def place_order(self, order: dict) -> dict:
        return self.request("POST", f"{API}/orders/sync", body=order)

    def modify_order(self, data: dict) -> dict:
        return self.request("PATCH", f"{API}/orders/sync", body=data)

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", f"{API}/orders/sync", body={"id": order_id})

    def exit_position(self, position_id: str | None = None) -> dict:
        body = {"id": position_id} if position_id else {"exit_all": 1}
        return self.request("DELETE", f"{API}/positions", body=body)

    # ---- market data (data role) ----------------------------------------
    def quotes(self, symbols: list[str]) -> dict:
        out: dict = {"s": "ok", "d": []}
        for i in range(0, len(symbols), 50):  # Fyers caps quotes at 50 symbols
            chunk = sorted(symbols[i:i + 50])
            r = self._cached_get(f"{DATA}/quotes", {"symbols": ",".join(chunk)}, self.quote_ttl)
            out["d"].extend(r.get("d", []))
        return out

    def history(self, symbol: str, resolution: str, range_from: int, range_to: int) -> dict:
        params = {
            "symbol": symbol, "resolution": resolution, "date_format": 0,
            "range_from": int(range_from), "range_to": int(range_to), "cont_flag": 1,
        }
        return self._cached_get(f"{DATA}/history", params, 5.0)

    def option_chain(self, symbol: str, strikecount: int = 10, expiry_ts: str = "") -> dict:
        params = {"symbol": symbol, "strikecount": strikecount, "timestamp": expiry_ts}
        return self._cached_get(f"{DATA}/options-chain-v3", params, 3.0)


def auth_url(app_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({"client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state})
    return f"{API}/generate-authcode?{q}"
