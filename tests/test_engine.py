import json
import time

import pytest

from viper.engine.features import Features, Signal, compute, detect, regime
from viper.engine.risk import RiskConfig, fees, size
from viper.journal import CSV_COLUMNS, build_filter, query, to_csv
from viper.learn.rules import check, extract_rules, overrides, parse_sentence
from viper.store.db import DB


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize("text,kind,params", [
    ("Avoid the first 15 minutes", "open_buffer", {"minutes": 15}),
    ("Don't trade in the last 20 min", "close_buffer", {"minutes": 20}),
    ("No trades between 12:30 and 1:30", "time_block", {"start": "12:30", "end": "13:30"}),
    ("No calls when RSI above 72", "indicator_ban", {"indicator": "rsi", "op": ">", "value": 72.0, "direction": 1}),
    ("Avoid trading when VIX above 20", "vix_max", {"value": 20.0}),
    ("Only buy puts below VWAP", "vwap_side", {"direction": -1}),
    ("Max 6 trades per day", "max_trades_day", {"value": 6}),
    ("Stop loss 1.5%", "stop_pct", {"value": 1.5}),
    ("Never hold more than 10 minutes", "max_hold", {"minutes": 10}),
    ("Avoid chop", "regime_ban", {"regime": "chop"}),
    ("Disable breakout trades", "strategy_ban", {"strategy": "breakout_pullback"}),
    ("Only buy calls", "direction_only", {"direction": 1}),
    ("Respect the trend, be patient", "note", {}),
])
def test_rule_parser(text, kind, params):
    r = parse_sentence(text)
    assert r["kind"] == kind
    assert r["params"] == params


def _feats(**kw):
    base = dict(key="NIFTY50", ts=time.time(), price=24300, ema9=24310, ema21=24290, twap=24280, atr=20, rsi=60,
                er=0.5, vix=13, day_high=24320, day_low=24200, minutes_since_open=60, minutes_to_close=120)
    base.update(kw)
    return Features(**base)


def test_rule_enforcement():
    rules = [{**r, "id": i} for i, r in enumerate(extract_rules("Avoid the first 15 minutes. No calls when RSI above 72. Avoid chop."))]
    sig = Signal("NIFTY50", "momentum_scalp", 1, 0.7, 0.6, 1.2, 5, "", "trending")
    assert check(rules, sig, _feats(), 0, time.time())[0]
    assert not check(rules, sig, _feats(minutes_since_open=5), 0, time.time())[0]
    assert not check(rules, sig, _feats(rsi=75), 0, time.time())[0]
    put = Signal("NIFTY50", "momentum_scalp", -1, 0.7, 0.6, 1.2, 5, "", "trending")
    assert check(rules, put, _feats(rsi=75), 0, time.time())[0]  # call-only ban
    chop = Signal("NIFTY50", "mean_reversion", 1, 0.7, 0.6, 1.2, 5, "", "chop")
    assert not check(rules, chop, _feats(), 0, time.time())[0]
    assert overrides(extract_rules("Max hold 7 minutes. Stop loss 0.5%")) == {"max_hold_min": 7, "max_sl_pct": 0.5}


# ------------------------------------------------------------------- risk
def test_sizing_respects_capital_and_stop_caps():
    cfg = RiskConfig(equity_allocation=200000, max_capital_pct=20, max_sl_pct=1)
    sig = Signal("NIFTY50", "momentum_scalp", 1, 0.7, stop_atr=0.6, target_atr=1.2, hold_min=5, reason="")
    s = size(sig, premium=100, lot=75, equity=200000, cfg=cfg, underlying_atr=20, offset=0)
    assert s and s.capital <= 200000 * 0.20 + 1e-6
    assert s.risk <= 200000 * 0.01 + 1e-6
    assert s.qty % 75 == 0 and s.stop_premium < 100 < s.target_premium


def test_sizing_tightens_stop_instead_of_skipping_when_one_lot_fits():
    cfg = RiskConfig(max_capital_pct=50, max_sl_pct=0.2)
    sig = Signal("BANKNIFTY", "breakout_pullback", 1, 0.7, stop_atr=1.5, target_atr=2, hold_min=10, reason="")
    s = size(sig, premium=300, lot=35, equity=100000, cfg=cfg, underlying_atr=80, offset=0)
    assert s and s.lots == 1 and s.risk <= 100000 * 0.002 + 1e-6 and s.note


def test_sizing_skips_when_unaffordable():
    cfg = RiskConfig(max_capital_pct=5)
    sig = Signal("SENSEX", "momentum_scalp", 1, 0.7, 0.6, 1.2, 5, "")
    assert size(sig, premium=500, lot=20, equity=50000, cfg=cfg, underlying_atr=60, offset=0) is None


def test_risk_config_validation():
    c = RiskConfig.from_dict({"max_capital_pct": 500, "max_sl_pct": -1, "automation": {"NIFTY50": "YOLO"}})
    assert c.max_capital_pct == 100 and c.max_sl_pct == 0.05 and c.automation["NIFTY50"] == "OFF"


def test_fees_are_positive_and_include_stt_on_sell():
    a = fees("NSE", 10000, 10000)
    b = fees("NSE", 10000, 20000)
    assert a > 40 and b > a


# --------------------------------------------------------------- features
def _bars(n, start=24000.0, step=2.0, t0=1_700_000_000, tf=60):
    out, p = [], start
    for i in range(n):
        o, c = p, p + step
        out.append({"t": t0 + i * tf, "o": o, "h": max(o, c) + 1, "l": min(o, c) - 1, "c": c, "v": 0})
        p = c
    return out


def test_trending_regime_and_detect_runs():
    b1 = _bars(60)
    b5 = _bars(60, step=0.4, tf=5)
    f = compute("NIFTY50", b1, b5, {"ltp": b1[-1]["c"], "ts": b1[-1]["t"] + 30}, 13, 0.3, False)
    assert f and f.ema9 > f.ema21 and regime(f) == "trending"
    sigs = detect(f, {})
    assert all(s.direction in (1, -1) and 0 < s.confidence <= 1 for s in sigs)


# ---------------------------------------------------------------- journal
@pytest.fixture()
def db(tmp_path):
    d = DB(tmp_path / "t.sqlite3")
    now = time.time()
    for i, (mode, inst) in enumerate([("live", "NIFTY50"), ("shadow", "NIFTY50"), ("shadow", "SENSEX")]):
        d.insert("trades", {"mode": mode, "instrument": inst, "symbol": f"S{i}", "side": 1, "qty": 75, "lots": 1,
                            "entry_ts": now - 600, "entry_price": 100, "exit_ts": now - 300, "exit_price": 110,
                            "gross_pnl": 750, "fees": 60, "net_pnl": 690, "strategy": "momentum_scalp",
                            "status": "closed", "meta": json.dumps({})})
    return d


def test_journal_filters_and_csv(db):
    assert len(query(db, mode="both", date_mode="today", instrument="ALL")) == 3
    assert len(query(db, mode="live", date_mode="all", instrument="ALL")) == 1
    assert len(query(db, mode="shadow", date_mode="all", instrument="SENSEX")) == 1
    rows = query(db, mode="both", date_mode="all", instrument="NIFTY50")
    csv = to_csv(rows)
    header = csv.splitlines()[0].split(",")
    assert header == CSV_COLUMNS
    assert len(csv.strip().splitlines()) == 3


def test_journal_filter_validation():
    with pytest.raises(ValueError):
        build_filter(mode="paper")
    with pytest.raises(ValueError):
        build_filter(date_mode="range", start="2026-11-05", end="2026-11-01")
