"""Synthetic staging feed. NEVER used for live orders.

Lets the full stack (charts, chain, shadow engine, learning) run on a laptop or CI
before FYERS tokens exist. Every payload it produces carries ``synthetic: true``
and the UI shows a persistent SYNTHETIC FEED banner. The execution layer refuses
real orders whenever the active feed is synthetic.

Spot paths are a regime-switching random walk; option prices are Black-Scholes on
that spot with a VIX-driven IV and a tick-size spread, so the shadow simulator
sees realistic bid/ask friction.
"""
from __future__ import annotations

import math
import random
import threading
import time
from datetime import date, datetime, timedelta

from viper.market.candles import IST
from viper.market.instruments import INSTRUMENTS, VIX_SYMBOL

_MONTH_CODE = "123456789OND"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, call: bool, r: float = 0.065) -> float:
    t = max(t_years, 1 / (365 * 24 * 60))
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if call:
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def synthetic_expiries(key: str, today: date | None = None, n: int = 4) -> list[date]:
    """Plausible weekly expiries for staging only (live uses Fyers' expiryData)."""
    today = today or datetime.now(IST).date()
    weekday = 3 if key == "SENSEX" else 1  # Thu for BSE, Tue for NSE
    d = today + timedelta(days=(weekday - today.weekday()) % 7)
    return [d + timedelta(weeks=i) for i in range(n)]


def option_symbol(key: str, expiry: date, strike: float, cp: str) -> str:
    inst = INSTRUMENTS[key]
    return f"{inst.fo_prefix}{expiry:%y}{_MONTH_CODE[expiry.month - 1]}{expiry:%d}{int(strike)}{cp}"


def parse_option_symbol(symbol: str) -> tuple[str, date, float, str] | None:
    for key in ("NIFTYNXT50", "BANKNIFTY", "SENSEX", "NIFTY50"):
        pre = INSTRUMENTS[key].fo_prefix
        if symbol.startswith(pre) and symbol[-2:] in ("CE", "PE"):
            body = symbol[len(pre):-2]
            try:
                yy, m, dd = int(body[:2]), _MONTH_CODE.index(body[2]) + 1, int(body[3:5])
                return key, date(2000 + yy, m, dd), float(body[5:]), symbol[-2:]
            except (ValueError, IndexError):
                return None
    return None


class SyntheticFeed:
    name = "synthetic"

    def __init__(self, on_tick, hz: float = 4.0, seed: int | None = None):
        self.on_tick = on_tick
        self.hz = hz
        self.rng = random.Random(seed)
        self.spots = {k: i.synthetic_seed for k, i in INSTRUMENTS.items()}
        self.prev_close = dict(self.spots)
        self.day_ohlc = {k: [v, v, v] for k, v in self.spots.items()}
        self.vix = 13.5
        self.drift = {k: 0.0 for k in INSTRUMENTS}
        self.symbols: set[str] = set()
        self.opt_state: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="synthetic-feed", daemon=True)
        self._thread.start()
        self.connected = True

    def stop(self) -> None:
        self._stop.set()
        self.connected = False

    def subscribe(self, symbols: list[str]) -> None:
        self.symbols.update(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
        self.symbols.difference_update(symbols)

    # -- history backfill ------------------------------------------------
    def history(self, key: str, tf_s: int, n: int, end_ts: float, end_price: float | None = None) -> list[dict]:
        """Random walk that ends at ``end_price`` (default: current spot), walked backwards."""
        rng = random.Random(hash((key, tf_s, int(end_ts) // max(tf_s, 1))) & 0xFFFFFF)
        price = end_price or self.spots[key]
        sigma = 0.16 / math.sqrt(252 * 22500) * math.sqrt(tf_s)
        out = []
        t = int(end_ts) - int(end_ts) % tf_s
        for _ in range(n):
            o = price * math.exp(rng.gauss(0, sigma))
            hi = max(o, price) * (1 + abs(rng.gauss(0, sigma / 2)))
            lo = min(o, price) * (1 - abs(rng.gauss(0, sigma / 2)))
            out.append({"t": t, "o": round(o, 2), "h": round(hi, 2), "l": round(lo, 2), "c": round(price, 2), "v": 0})
            price = o
            t -= tf_s
        out.reverse()
        return out

    # -- tick loop -------------------------------------------------------
    def _run(self) -> None:
        dt = 1 / self.hz
        sigma = 0.17 / math.sqrt(252 * 22500) * math.sqrt(dt)
        while not self._stop.is_set():
            now = time.time()
            self.vix = max(9.0, min(30.0, self.vix + self.rng.gauss(0, 0.004)))
            for k in INSTRUMENTS:
                if self.rng.random() < 0.002:  # occasional regime shift
                    self.drift[k] = self.rng.choice([-1, 0, 0, 1]) * sigma * 0.08
                ret = self.drift[k] + self.rng.gauss(0, sigma)
                s = self.spots[k] = round(self.spots[k] * math.exp(ret), 2)
                o = self.day_ohlc[k]
                o[1], o[2] = max(o[1], s), min(o[2], s)
                self.on_tick({
                    "symbol": INSTRUMENTS[k].spot, "ltp": s, "bid": None, "ask": None,
                    "open": o[0], "high": o[1], "low": o[2], "prev_close": self.prev_close[k],
                    "vol": 0, "ts": now, "synthetic": True,
                })
            self.on_tick({"symbol": VIX_SYMBOL, "ltp": round(self.vix, 2), "open": 13.5, "high": 14, "low": 13,
                          "prev_close": 13.5, "vol": 0, "ts": now, "synthetic": True})
            for sym in list(self.symbols):
                tick = self._option_tick(sym, now)
                if tick:
                    self.on_tick(tick)
            self._stop.wait(dt)

    def _option_tick(self, sym: str, now: float) -> dict | None:
        p = parse_option_symbol(sym)
        if not p:
            return None
        key, exp, strike, cp = p
        exp_dt = datetime(exp.year, exp.month, exp.day, 15, 30, tzinfo=IST)
        t = max((exp_dt.timestamp() - now) / (365 * 86400), 1e-5)
        spot = self.spots[key]
        iv = self.vix / 100 * (1 + 0.6 * abs(math.log(strike / spot)) * 10)
        px = max(0.05, bs_price(spot, strike, t, iv, cp == "CE"))
        px = round(round(px / 0.05) * 0.05, 2)
        spread = max(0.05, round(px * 0.0015 / 0.05) * 0.05)
        st = self.opt_state.setdefault(sym, {"o": px, "h": px, "l": px, "pc": round(px * (1 + self.rng.gauss(0, 0.04)), 2), "vol": 0})
        st["h"], st["l"] = max(st["h"], px), min(st["l"], px)
        st["vol"] += self.rng.choice([0, 0, 25, 50, 75, 150]) * 5
        return {
            "symbol": sym, "ltp": px, "bid": round(px - spread / 2, 2), "ask": round(px + spread / 2, 2),
            "open": st["o"], "high": st["h"], "low": st["l"], "prev_close": st["pc"],
            "vol": st["vol"], "tbq": self.rng.randint(1000, 90000), "tsq": self.rng.randint(1000, 90000),
            "oi": 0, "ts": now, "synthetic": True,
        }

    def option_chain(self, key: str, strikecount: int, expiry: date | None) -> dict:
        exps = synthetic_expiries(key)
        exp = expiry or exps[0]
        spot = self.spots[key]
        step = INSTRUMENTS[key].strike_step
        atm = round(spot / step) * step
        strikes = [atm + i * step for i in range(-strikecount, strikecount + 1)]
        return {
            "expiries": [{"date": d.isoformat(), "ts": str(int(datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()))} for d in exps],
            "expiry": exp.isoformat(),
            "spot": spot,
            "rows": [{"strike": k, "CE": option_symbol(key, exp, k, "CE"), "PE": option_symbol(key, exp, k, "PE")} for k in strikes],
        }
