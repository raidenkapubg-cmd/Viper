"""Instruction -> structured rule extraction, and rule enforcement.

Rule kinds the engine enforces (``enforced=1``):

  time_block      {start:"HH:MM", end:"HH:MM"}            no new entries inside window
  open_buffer     {minutes:N}                              no entries in first N min
  close_buffer    {minutes:N}                              no entries in last N min
  direction_only  {direction:+1|-1}                        only calls (+1) / only puts (-1)
  direction_ban   {direction:+1|-1}
  indicator_ban   {indicator, op, value, direction|0}      e.g. no calls when RSI > 70
  vwap_side       {direction}                              calls only above TWAP / puts only below
  regime_ban      {regime}
  strategy_ban    {strategy}
  vix_max / vix_min {value}
  max_trades_day  {value}
  max_hold        {minutes}                                caps hold time for this scope
  stop_pct        {value}                                  caps Max SL % for this scope
  min_confidence  {value}

Anything else is stored as ``note`` (shown in the UI, not machine-enforced), so
nothing a user writes is silently dropped or falsely claimed as enforced.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from viper.market.candles import IST

STRATEGY_WORDS = {
    "momentum": "momentum_scalp", "scalp": "momentum_scalp",
    "imbalance": "ofi_imbalance", "order flow": "ofi_imbalance", "order-flow": "ofi_imbalance",
    "breakout": "breakout_pullback", "pullback": "breakout_pullback",
    "mean reversion": "mean_reversion", "mean-reversion": "mean_reversion", "reversion": "mean_reversion",
}
REGIME_WORDS = {
    "chop": "chop", "choppy": "chop", "sideways": "chop", "range": "chop", "rangebound": "chop",
    "trend": "trending", "trending": "trending",
    "high iv": "high_iv", "high volatility": "high_iv", "volatile": "high_iv",
    "expiry": "expiry_gamma", "expiry day": "expiry_gamma",
}
TIME = r"(\d{1,2})[:.](\d{2})\s*(am|pm)?"
NEG = r"(?:no|don'?t|do not|never|avoid|stop|skip|block|disable|turn off|pause)"


def _hm(h: str, m: str, ap: str | None) -> str:
    hh = int(h)
    if ap:
        ap = ap.lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
    elif hh < 8:  # "2:30" in a trading context means 14:30
        hh += 12
    return f"{hh:02d}:{int(m):02d}"


def _dir(s: str) -> int | None:
    calls = re.search(r"\b(calls?|ce|bullish|longs?)\b", s)
    puts = re.search(r"\b(puts?|pe|bearish|shorts?)\b", s)
    if calls and not puts:
        return 1
    if puts and not calls:
        return -1
    return None


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+|•|•|^\s*[-*]\s+", text, flags=re.M)
    return [p.strip(" -*\t") for p in parts if p and len(p.strip()) > 3]


def parse_sentence(s: str) -> dict:
    t = s.lower()
    rule: dict = {"text": s.strip()}

    m = re.search(rf"between\s+{TIME}\s+(?:and|to|-)\s+{TIME}", t)
    if m and re.search(NEG, t):
        a = _hm(*m.group(1, 2, 3))
        b = _hm(*m.group(4, 5, 6))
        return {**rule, "kind": "time_block", "params": {"start": a, "end": b}}
    m = re.search(rf"{NEG}.*\b(before)\s+{TIME}", t)
    if m:
        return {**rule, "kind": "time_block", "params": {"start": "00:00", "end": _hm(*m.group(2, 3, 4))}}
    m = re.search(rf"{NEG}.*\b(after)\s+{TIME}", t)
    if m:
        return {**rule, "kind": "time_block", "params": {"start": _hm(*m.group(2, 3, 4)), "end": "23:59"}}
    m = re.search(r"first\s+(\d+)\s*(?:min|minutes|mins|m)\b", t)
    if m and re.search(NEG + r"|wait", t):
        return {**rule, "kind": "open_buffer", "params": {"minutes": int(m.group(1))}}
    m = re.search(r"last\s+(\d+)\s*(?:min|minutes|mins|m)\b", t)
    if m and re.search(NEG, t):
        return {**rule, "kind": "close_buffer", "params": {"minutes": int(m.group(1))}}

    m = re.search(r"(rsi|vix)\s*(?:is\s*)?(above|over|greater than|>|below|under|less than|<)\s*(\d+(?:\.\d+)?)", t)
    if m and re.search(NEG, t):
        ind, op_w, val = m.group(1), m.group(2), float(m.group(3))
        op = ">" if op_w in ("above", "over", "greater than", ">") else "<"
        d = _dir(t)
        if ind == "vix" and d is None:
            return {**rule, "kind": "vix_max" if op == ">" else "vix_min", "params": {"value": val}}
        return {**rule, "kind": "indicator_ban", "params": {"indicator": ind, "op": op, "value": val, "direction": d or 0}}

    if re.search(r"\b(vwap|twap)\b", t) and _dir(t) is not None:
        d = _dir(t)
        if re.search(r"only|above|below", t):
            return {**rule, "kind": "vwap_side", "params": {"direction": d}}

    m = re.search(r"(?:max(?:imum)?|at most|no more than|upto|up to)\s+(\d+)\s+trades?", t)
    if m:
        return {**rule, "kind": "max_trades_day", "params": {"value": int(m.group(1))}}
    m = re.search(r"(?:stop[\s-]?loss|sl)\s*(?:of|at|=|:)?\s*(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return {**rule, "kind": "stop_pct", "params": {"value": float(m.group(1))}}
    m = re.search(r"(?:hold|holding).{0,30}?(\d+)\s*(?:min|minutes|mins|m)\b", t)
    if m and re.search(r"max|more than|longer|exceed|over|at most|never", t):
        return {**rule, "kind": "max_hold", "params": {"minutes": int(m.group(1))}}
    m = re.search(r"confidence\s*(?:above|over|>|of at least|>=)\s*(\d+(?:\.\d+)?)\s*(%?)", t)
    if m:
        v = float(m.group(1))
        return {**rule, "kind": "min_confidence", "params": {"value": v / 100 if m.group(2) or v > 1 else v}}

    if re.search(NEG, t):
        for w, strat in STRATEGY_WORDS.items():
            if w in t:
                return {**rule, "kind": "strategy_ban", "params": {"strategy": strat}}
        for w, rg in REGIME_WORDS.items():
            if re.search(rf"\b{re.escape(w)}\b", t):
                return {**rule, "kind": "regime_ban", "params": {"regime": rg}}
        d = _dir(t)
        if d is not None and re.search(r"\b(buy|buying|take|taking|trade|trading|long)\b", t):
            return {**rule, "kind": "direction_ban", "params": {"direction": d}}
    if re.search(r"\bonly\b", t):
        d = _dir(t)
        if d is not None:
            return {**rule, "kind": "direction_only", "params": {"direction": d}}
    return {**rule, "kind": "note", "params": {}}


def extract_rules(text: str) -> list[dict]:
    return [parse_sentence(s) for s in split_sentences(text)]


# ---------------------------------------------------------------- enforcement
def _in_window(hm: str, start: str, end: str) -> bool:
    return start <= hm <= end if start <= end else (hm >= start or hm <= end)


def check(rules: list[dict], sig, feats, trades_today: int, now_ts: float) -> tuple[bool, str]:
    """Return (allowed, reason). ``rules`` are DB rows already filtered by scope."""
    hm = datetime.fromtimestamp(now_ts, IST).strftime("%H:%M")
    for r in rules:
        k, p = r["kind"], r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])
        why = f"rule #{r.get('id', '?')}: {r['text'][:80]}"
        if k == "time_block" and _in_window(hm, p["start"], p["end"]):
            return False, why
        if k == "open_buffer" and feats.minutes_since_open < p["minutes"]:
            return False, why
        if k == "close_buffer" and feats.minutes_to_close < p["minutes"]:
            return False, why
        if k == "direction_only" and sig.direction != p["direction"]:
            return False, why
        if k == "direction_ban" and sig.direction == p["direction"]:
            return False, why
        if k == "strategy_ban" and sig.strategy == p["strategy"]:
            return False, why
        if k == "regime_ban" and sig.regime == p["regime"]:
            return False, why
        if k == "vix_max" and feats.vix is not None and feats.vix > p["value"]:
            return False, why
        if k == "vix_min" and feats.vix is not None and feats.vix < p["value"]:
            return False, why
        if k == "max_trades_day" and trades_today >= p["value"]:
            return False, why
        if k == "min_confidence" and sig.confidence < p["value"]:
            return False, why
        if k == "vwap_side" and feats.twap is not None and sig.direction == p["direction"]:
            if (feats.price - feats.twap) * p["direction"] < 0:
                return False, why
        if k == "indicator_ban":
            val = feats.rsi if p["indicator"] == "rsi" else feats.vix
            if val is not None and (p["direction"] in (0, sig.direction)):
                hit = val > p["value"] if p["op"] == ">" else val < p["value"]
                if hit:
                    return False, why
    return True, ""


def overrides(rules: list[dict]) -> dict:
    """Numeric caps rules impose on the risk config for their scope."""
    out: dict = {}
    for r in rules:
        p = r["params"] if isinstance(r["params"], dict) else json.loads(r["params"])
        if r["kind"] == "max_hold":
            out["max_hold_min"] = min(out.get("max_hold_min", 1e9), p["minutes"])
        if r["kind"] == "stop_pct":
            out["max_sl_pct"] = min(out.get("max_sl_pct", 1e9), p["value"])
    return out
