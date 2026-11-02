"""Tick -> candle aggregation for every UI timeframe, plus sanitisation.

Sub-minute frames (1s/5s/30s) exist only from the live tick stream; minute-and-up
frames are backfilled from REST history and continued from ticks. ``stitch``
merges the two so a chart never shows a seam.

``sanitize`` is the server half of the flat-chart fix: any candle carrying a 0,
null or NaN price is dropped (one zero among values near 80,000 squashes the
whole series into a flat line on any auto-scaled axis).
"""
from __future__ import annotations

import math
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

TIMEFRAMES: dict[str, int] = {
    "1s": 1, "5s": 5, "30s": 30, "1m": 60, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
}
# Fyers v3 history resolutions. Second resolutions are only served for recent
# sessions; when Fyers rejects them we fall back to tick-built candles alone.
FYERS_RESOLUTION = {
    "1s": "1S", "5s": "5S", "30s": "30S", "1m": "1", "5m": "5", "15m": "15",
    "30m": "30", "1h": "60", "4h": "240", "1d": "1D",
}
MAX_BARS = 3000


def _valid(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def sanitize(candles: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in candles:
        o, h, l, cl = c.get("o"), c.get("h"), c.get("l"), c.get("c")
        if not all(_valid(v) for v in (o, h, l, cl)) or not c.get("t"):
            continue
        hi, lo = max(o, h, l, cl), min(o, h, l, cl)
        v = c.get("v") or 0
        out.append({"t": int(c["t"]), "o": o, "h": hi, "l": lo, "c": cl, "v": v if _valid(v) else 0})
    out.sort(key=lambda c: c["t"])
    dedup: list[dict] = []
    for c in out:
        if dedup and dedup[-1]["t"] == c["t"]:
            dedup[-1] = c
        else:
            dedup.append(c)
    return dedup


def bucket_start(ts: float, tf_s: int) -> int:
    """Bucket start (epoch s). Intraday frames >= 1m anchor to the 09:15 IST session open,
    matching how Fyers stamps 30m/1h/4h candles; 1d anchors to IST midnight."""
    t = int(ts)
    if tf_s < 60:
        return t - t % tf_s
    d = datetime.fromtimestamp(t, IST)
    midnight = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    if tf_s >= 86400:
        return midnight
    anchor = midnight + 9 * 3600 + 15 * 60
    if t < anchor:
        return t - (t - midnight) % tf_s
    return anchor + ((t - anchor) // tf_s) * tf_s


def from_fyers_history(payload: dict) -> list[dict]:
    rows = payload.get("candles") or []
    return sanitize([{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5] if len(r) > 5 else 0} for r in rows])


def stitch(history: list[dict], live: list[dict]) -> list[dict]:
    if not live:
        return sanitize(history)
    first_live = live[0]["t"]
    merged = [c for c in history if c["t"] < first_live] + live
    return sanitize(merged)[-MAX_BARS:]


class SeriesAggregator:
    """Builds candles for one symbol across all timeframes from ticks."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bars: dict[str, deque] = {tf: deque(maxlen=MAX_BARS) for tf in TIMEFRAMES}
        self._last_cum_vol: float | None = None
        self.lock = threading.Lock()

    def on_tick(self, ts: float, price: float, cum_vol: float | None = None) -> list[tuple[str, dict, bool]]:
        """Returns [(tf, candle, closed_previous)] for every frame touched."""
        if not _valid(price):
            return []
        dv = 0.0
        if cum_vol is not None and cum_vol > 0:
            if self._last_cum_vol is not None and cum_vol >= self._last_cum_vol:
                dv = cum_vol - self._last_cum_vol
            self._last_cum_vol = cum_vol
        out = []
        with self.lock:
            for tf, sec in TIMEFRAMES.items():
                start = bucket_start(ts, sec)
                dq = self.bars[tf]
                closed = False
                if dq and dq[-1]["t"] == start:
                    c = dq[-1]
                    c["h"] = max(c["h"], price)
                    c["l"] = min(c["l"], price)
                    c["c"] = price
                    c["v"] += dv
                elif dq and dq[-1]["t"] > start:
                    continue  # late tick for an already-rolled bucket
                else:
                    closed = bool(dq)
                    c = {"t": start, "o": price, "h": price, "l": price, "c": price, "v": dv}
                    dq.append(c)
                out.append((tf, dict(c), closed))
        return out

    def backfill(self, tf: str, candles: list[dict]) -> int:
        """Merge historical candles *in front of* tick-built bars (never over them)."""
        with self.lock:
            dq = self.bars[tf]
            existing = list(dq)
            first = existing[0]["t"] if existing else None
            hist = [dict(c) for c in sanitize(candles) if first is None or c["t"] < first]
            if not hist:
                return 0
            dq.clear()
            for c in (hist + existing)[-MAX_BARS:]:
                dq.append(c)
            return len(hist)

    def get(self, tf: str, n: int = 500) -> list[dict]:
        with self.lock:
            return [dict(c) for c in list(self.bars[tf])[-n:]]

    def closed(self, tf: str, n: int) -> list[dict]:
        """Last ``n`` *closed* bars (excludes the forming one)."""
        with self.lock:
            bars = list(self.bars[tf])
        return [dict(c) for c in bars[:-1][-n:]]
