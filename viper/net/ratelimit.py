"""Request pacing that never invents "API limit reached" errors.

Design rules (the old false-limit bug came from breaking these):

* The client never *rejects* a request because of its own counters. The token
  bucket only smooths bursts by *waiting* a few milliseconds.
* Identical concurrent GETs are coalesced (single-flight): N callers asking for the
  same quotes at once produce one HTTP request.
* Short TTL caching for idempotent market reads so UI refreshes can't fan out.
* Backoff happens only when Fyers actually answers HTTP 429 (or its JSON body
  reports a rate-limit code), honouring ``Retry-After`` when present.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Hashable


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = max(rate_per_sec, 0.1)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._ts = time.monotonic()
        self._lock = threading.Lock()
        self.waited_ms = 0.0

    def acquire(self) -> float:
        """Take one token, sleeping if needed. Returns seconds waited. Never raises."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
            self._ts = now
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            wait = (1 - self._tokens) / self.rate
            self._tokens = 0.0
            self._ts = now + wait
        time.sleep(wait)
        self.waited_ms += wait * 1000
        return wait


class _Call:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class SingleFlight:
    """Collapse concurrent identical calls into one execution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[Hashable, _Call] = {}
        self.coalesced = 0

    def do(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = self._calls[key] = _Call()
            else:
                self.coalesced += 1
        assert call is not None
        if not leader:
            call.event.wait()
            if call.error:
                raise call.error
            return call.result
        try:
            call.result = fn()
            return call.result
        except BaseException as e:
            call.error = e
            raise
        finally:
            with self._lock:
                self._calls.pop(key, None)
            call.event.set()


class TTLCache:
    def __init__(self) -> None:
        self._d: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any:
        with self._lock:
            hit = self._d.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            return None

    def put(self, key: Hashable, value: Any, ttl_s: float) -> None:
        with self._lock:
            self._d[key] = (time.monotonic() + ttl_s, value)
            if len(self._d) > 2048:
                now = time.monotonic()
                for k in [k for k, (exp, _) in self._d.items() if exp <= now]:
                    del self._d[k]


@dataclass
class BackoffPolicy:
    base_s: float = 0.5
    cap_s: float = 16.0
    max_retries: int = 5

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.cap_s * 4)
            except ValueError:
                pass
        # Deterministic exponential with small spread derived from the attempt number.
        d = min(self.cap_s, self.base_s * (2 ** attempt))
        return d * (0.85 + 0.3 * ((attempt * 7919) % 10) / 10)
