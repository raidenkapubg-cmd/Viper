"""Guardrails, position sizing and Indian F&O charges.

Definitions used everywhere (UI tooltips repeat them):

* **Max Capital Use %**: premium outlay of one trade <= pct x deployable equity.
* **Max Stop Loss %**: worst-case loss of one trade (qty x (entry - stop)) <= pct x
  deployable equity. The premium stop derived from the setup's underlying stop is
  tightened if needed so this holds.
* **Max Hold (min)**: the trade is squared off when this elapses.
* **Deployable equity** = min(user's Equity Allocation, live available margin)
  for live; the shadow book's own equity for shadow.

Viper only *buys* options (long CE / long PE), so loss per trade is bounded by the
premium even if a stop gaps.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from viper.engine.features import Signal

AUTOMATION_STATES = ("ON", "OFF", "AUTO")


@dataclass
class RiskConfig:
    equity_allocation: float = 200000.0
    max_capital_pct: float = 20.0
    max_sl_pct: float = 1.0
    max_hold_min: float = 15.0
    daily_loss_pct: float = 3.0
    max_open_live: int = 4
    cooldown_s: int = 60
    automation: dict = field(default_factory=lambda: {k: "OFF" for k in ("NIFTY50", "NIFTYNXT50", "SENSEX", "BANKNIFTY")})
    kill_switch: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskConfig":
        base = cls()
        if not d:
            return base
        for k, v in d.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base.validated()

    def validated(self) -> "RiskConfig":
        self.equity_allocation = max(0.0, float(self.equity_allocation))
        self.max_capital_pct = min(100.0, max(0.1, float(self.max_capital_pct)))
        self.max_sl_pct = min(50.0, max(0.05, float(self.max_sl_pct)))
        self.max_hold_min = min(375.0, max(0.5, float(self.max_hold_min)))
        self.daily_loss_pct = min(100.0, max(0.1, float(self.daily_loss_pct)))
        self.max_open_live = max(0, int(self.max_open_live))
        for k, v in list(self.automation.items()):
            if v not in AUTOMATION_STATES:
                self.automation[k] = "OFF"
        return self

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SizedOrder:
    lots: int
    qty: int
    premium: float
    stop_premium: float
    target_premium: float
    capital: float
    risk: float
    strike_offset: int
    note: str


def estimate_delta(offset_steps: int) -> float:
    """Rough |delta| by moneyness step (0 = ATM). Only used to map an underlying
    stop to a premium stop; real fills are what PnL is computed from."""
    return {-2: 0.72, -1: 0.62, 0: 0.5, 1: 0.38, 2: 0.27}.get(offset_steps, 0.5)


def strike_plan(regime: str, auto: bool) -> list[int]:
    """Preferred moneyness order (0 ATM, +n OTM, -n ITM). Tried until one fits the budget."""
    if not auto:
        return [0, 1, 2]
    return {
        "trending": [1, 0, 2],      # cheaper gamma in trend
        "high_iv": [-1, 0, 1],      # ITM: less vega bleed when IV is rich
        "expiry_gamma": [0, 1],     # ATM: theta burns OTM fastest on expiry
        "chop": [0, -1, 1],
    }.get(regime, [0, 1, 2])


def size(sig: Signal, premium: float, lot: int, equity: float, cfg: RiskConfig, underlying_atr: float,
         offset: int, size_mult: float = 1.0, tick: float = 0.05) -> SizedOrder | None:
    if premium <= 0 or lot <= 0 or equity <= 0:
        return None
    cap_budget = equity * cfg.max_capital_pct / 100
    risk_budget = equity * cfg.max_sl_pct / 100
    delta = estimate_delta(offset)
    stop_pts = max(tick * 2, delta * sig.stop_atr * underlying_atr)
    stop_pts = min(stop_pts, premium * 0.6)  # never plan a stop below 40% of premium
    target_pts = max(tick * 3, delta * sig.target_atr * underlying_atr)
    lots_cap = math.floor(cap_budget / (premium * lot))
    lots_risk = math.floor(risk_budget / (stop_pts * lot))
    lots = int(min(lots_cap, lots_risk) * max(0.25, min(size_mult, 1.5)))
    note = ""
    if lots < 1 and lots_cap >= 1:
        # One lot is affordable but the setup's stop is too wide for the SL budget:
        # tighten the stop to the budget rather than skip.
        stop_pts = risk_budget / lot
        if stop_pts < tick * 2:
            return None
        lots, note = 1, "stop tightened to fit Max SL %"
    if lots < 1:
        return None
    qty = lots * lot
    return SizedOrder(
        lots=lots, qty=qty, premium=premium,
        stop_premium=round(max(tick, premium - stop_pts) / tick) * tick,
        target_premium=round((premium + target_pts) / tick) * tick,
        capital=round(premium * qty, 2), risk=round(stop_pts * qty, 2),
        strike_offset=offset, note=note,
    )


def fees(exchange: str, buy_value: float, sell_value: float, orders: int = 2) -> float:
    """Approximate charges for an intraday index option round trip (INR).
    Brokerage flat 20/order, STT 0.1% on sell premium, exchange txn charges
    (NSE 0.03503%, BSE 0.0325%), SEBI 10/crore, stamp 0.003% on buy, GST 18%."""
    brokerage = 20.0 * orders
    stt = 0.001 * sell_value
    txn_rate = 0.0003503 if exchange == "NSE" else 0.000325
    txn = txn_rate * (buy_value + sell_value)
    sebi = 10 / 1e7 * (buy_value + sell_value)
    stamp = 0.00003 * buy_value
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)
