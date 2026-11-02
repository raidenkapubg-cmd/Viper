"""Indicators, regime tagging and the four setup detectors.

Index spot symbols carry no volume on Fyers, so ``twap`` (session time-weighted
average of typical price) stands in for VWAP on the underlying; order-flow
imbalance is read from the ATM option books (total buy vs sell quantity).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from viper.market.candles import IST


def ema(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals: list[float], n: int = 14) -> float | None:
    if len(vals) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = []
    for p, c in zip(bars[-n - 1:-1], bars[-n:]):
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return sum(trs) / n


@dataclass
class Features:
    key: str
    ts: float
    price: float
    ema9: float | None = None
    ema21: float | None = None
    twap: float | None = None
    atr: float | None = None
    rsi: float | None = None
    er: float | None = None           # Kaufman efficiency ratio (0 chop .. 1 trend)
    rv: float | None = None           # realised vol of 1m returns (annualised)
    mom_z: float | None = None        # 5s-bar momentum z-score over the last minute
    mom_slope: float | None = None    # change in momentum vs previous minute
    hh20: float | None = None
    ll20: float | None = None
    breakout_age: int | None = None   # bars since close crossed above hh20 (+) / below ll20 (-)
    breakout_level: float | None = None
    ofi: float | None = None          # ATM CE book imbalance minus ATM PE book imbalance
    vix: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    minutes_since_open: float = 0
    minutes_to_close: float = 0
    expiry_day: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def compute(key: str, bars1m: list[dict], bars5s: list[dict], tick: dict, vix: float | None,
            ofi: float | None, expiry_day: bool, open_hm=(9, 15), close_hm=(15, 30)) -> Features | None:
    if len(bars1m) < 22 or not tick:
        return None
    closes = [b["c"] for b in bars1m]
    price = tick["ltp"]
    now = datetime.fromtimestamp(tick.get("ts") or 0, IST)
    open_dt = now.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    close_dt = now.replace(hour=close_hm[0], minute=close_hm[1], second=0, microsecond=0)
    f = Features(key=key, ts=tick.get("ts", 0), price=price, vix=vix, ofi=ofi, expiry_day=expiry_day)
    f.minutes_since_open = (now - open_dt).total_seconds() / 60
    f.minutes_to_close = (close_dt - now).total_seconds() / 60
    f.ema9, f.ema21 = ema(closes, 9), ema(closes, 21)
    f.atr = atr(bars1m, 14)
    f.rsi = rsi(closes, 14)
    session = [b for b in bars1m if b["t"] >= open_dt.timestamp()] or bars1m[-60:]
    f.twap = sum((b["h"] + b["l"] + b["c"]) / 3 for b in session) / len(session)
    f.day_high = tick.get("high") or max(b["h"] for b in session)
    f.day_low = tick.get("low") or min(b["l"] for b in session)
    w = closes[-11:]
    path = sum(abs(b - a) for a, b in zip(w, w[1:]))
    f.er = abs(w[-1] - w[0]) / path if path > 0 else 0.0
    rets = [math.log(b / a) for a, b in zip(closes[-31:], closes[-30:]) if a > 0 and b > 0]
    if len(rets) > 5:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
        f.rv = sd * math.sqrt(252 * 375)
    prior = bars1m[-21:-1]
    f.hh20, f.ll20 = max(b["h"] for b in prior), min(b["l"] for b in prior)
    # breakout tracking over the last 8 bars
    for age in range(1, 9):
        if len(bars1m) < 22 + age:
            break
        ref = bars1m[-21 - age:-1 - age]
        hh, ll = max(b["h"] for b in ref), min(b["l"] for b in ref)
        c = bars1m[-age]["c"]
        if c > hh:
            f.breakout_age, f.breakout_level = age, hh
            break
        if c < ll:
            f.breakout_age, f.breakout_level = -age, ll
            break
    if len(bars5s) >= 30:
        r5 = [b["c"] / a["c"] - 1 for a, b in zip(bars5s[:-1], bars5s[1:]) if a["c"] > 0]
        if len(r5) >= 24:
            last12, prev = r5[-12:], r5[:-12]
            sd = math.sqrt(sum(x * x for x in prev) / len(prev)) or 1e-9
            f.mom_z = sum(last12) / (sd * math.sqrt(12))
            prev12 = r5[-24:-12]
            f.mom_slope = f.mom_z - sum(prev12) / (sd * math.sqrt(12))
    return f


def regime(f: Features) -> str:
    if f.expiry_day and f.minutes_to_close < 150:
        return "expiry_gamma"
    if (f.vix and f.vix >= 18) or (f.rv and f.rv >= 0.28):
        return "high_iv"
    if f.er is not None and f.atr and f.ema9 and f.ema21:
        if f.er >= 0.45 and abs(f.ema9 - f.ema21) / f.atr >= 0.3:
            return "trending"
    return "chop"


@dataclass
class Signal:
    key: str
    strategy: str
    direction: int          # +1 bullish (buy CE), -1 bearish (buy PE)
    confidence: float       # 0..1 heuristic quality before learning
    stop_atr: float         # stop distance on the underlying, in ATRs
    target_atr: float
    hold_min: float
    reason: str
    regime: str = ""


def detect(f: Features, params: dict) -> list[Signal]:
    """Run all setups. ``params`` holds learner patches per strategy."""
    out: list[Signal] = []
    if not f.atr or f.atr <= 0 or f.ema9 is None or f.ema21 is None:
        return out
    trend = 1 if f.ema9 > f.ema21 else -1
    p = lambda s, k, d: params.get(s, {}).get(k, d)  # noqa: E731

    # 1. momentum scalp: sharp 5s impulse aligned with the 1m EMA trend
    if f.mom_z is not None and abs(f.mom_z) >= p("momentum_scalp", "z", 1.6):
        d = 1 if f.mom_z > 0 else -1
        if d == trend:
            conf = min(0.9, 0.45 + 0.08 * abs(f.mom_z) + (0.1 if (f.mom_slope or 0) * d > 0 else -0.05))
            out.append(Signal(f.key, "momentum_scalp", d, conf, p("momentum_scalp", "stop_atr", 0.6),
                              p("momentum_scalp", "target_atr", 1.2), p("momentum_scalp", "hold_min", 5),
                              f"5s momentum z={f.mom_z:.2f} with EMA9{'>' if d > 0 else '<'}EMA21"))

    # 2. order-flow imbalance on the ATM books, confirmed by price vs TWAP
    if f.ofi is not None and abs(f.ofi) >= p("ofi_imbalance", "min_ofi", 0.22) and f.twap:
        d = 1 if f.ofi > 0 else -1
        if (f.price - f.twap) * d > 0:
            conf = min(0.85, 0.45 + abs(f.ofi) * 0.6)
            out.append(Signal(f.key, "ofi_imbalance", d, conf, p("ofi_imbalance", "stop_atr", 0.7),
                              p("ofi_imbalance", "target_atr", 1.3), p("ofi_imbalance", "hold_min", 8),
                              f"ATM book imbalance {f.ofi:+.2f}, price {'above' if d > 0 else 'below'} TWAP"))

    # 3. breakout pullback: fresh 20-bar breakout, price retests the level and holds
    if f.breakout_age and f.breakout_level and 2 <= abs(f.breakout_age) <= 8:
        d = 1 if f.breakout_age > 0 else -1
        dist = (f.price - f.breakout_level) * d / f.atr
        if -0.15 <= dist <= p("breakout_pullback", "max_retest_atr", 0.45):
            conf = 0.55 + (0.1 if d == trend else -0.1) + (0.05 if (f.er or 0) > 0.4 else 0)
            out.append(Signal(f.key, "breakout_pullback", d, conf, p("breakout_pullback", "stop_atr", 0.8),
                              p("breakout_pullback", "target_atr", 1.8), p("breakout_pullback", "hold_min", 15),
                              f"retest of 20-bar {'high' if d > 0 else 'low'} {f.breakout_level:.1f} ({abs(f.breakout_age)} bars ago)"))

    # 4. TWAP/EMA mean reversion: stretched + RSI extreme, only outside strong trends
    if f.twap and f.rsi is not None and (f.er or 0) < 0.5:
        stretch = (f.price - f.twap) / f.atr
        lim = p("mean_reversion", "stretch_atr", 1.6)
        if stretch >= lim and f.rsi >= 70:
            out.append(Signal(f.key, "mean_reversion", -1, min(0.8, 0.4 + 0.08 * stretch), p("mean_reversion", "stop_atr", 0.7),
                              p("mean_reversion", "target_atr", 1.0), p("mean_reversion", "hold_min", 10),
                              f"{stretch:.1f} ATR above TWAP, RSI {f.rsi:.0f}"))
        elif stretch <= -lim and f.rsi <= 30:
            out.append(Signal(f.key, "mean_reversion", 1, min(0.8, 0.4 + 0.08 * -stretch), p("mean_reversion", "stop_atr", 0.7),
                              p("mean_reversion", "target_atr", 1.0), p("mean_reversion", "hold_min", 10),
                              f"{-stretch:.1f} ATR below TWAP, RSI {f.rsi:.0f}"))
    rg = regime(f)
    for s in out:
        s.regime = rg
    return out


STRATEGIES = ["momentum_scalp", "ofi_imbalance", "breakout_pullback", "mean_reversion"]
REGIMES = ["trending", "chop", "high_iv", "expiry_gamma"]
