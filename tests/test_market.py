from datetime import datetime

from viper.market.candles import IST, SeriesAggregator, bucket_start, from_fyers_history, sanitize, stitch
from viper.market.chain import COLUMN_LABELS, COLUMNS, cells_from_tick, parse_fyers_chain
from viper.market.instruments import atm_strike, instrument_for_symbol
from viper.market.synthetic import option_symbol, parse_option_symbol


def ts(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=IST).timestamp()


def test_sanitize_strips_zero_null_nan_that_flatten_charts():
    bars = [
        {"t": 1, "o": 80000, "h": 80100, "l": 79950, "c": 80050},
        {"t": 2, "o": 0, "h": 0, "l": 0, "c": 0},
        {"t": 3, "o": None, "h": 80100, "l": 80000, "c": 80020},
        {"t": 4, "o": float("nan"), "h": 1, "l": 1, "c": 1},
        {"t": 5, "o": 80040, "h": 80030, "l": 80090, "c": 80060},  # h/l swapped
    ]
    out = sanitize(bars)
    assert [b["t"] for b in out] == [1, 5]
    assert out[1]["h"] == 80090 and out[1]["l"] == 80030
    lo, hi = min(b["l"] for b in out), max(b["h"] for b in out)
    assert lo > 79000 and hi < 81000  # tight domain, no zero anchor


def test_bucket_anchor_matches_fyers_session():
    assert bucket_start(ts(9, 47), 1800) == ts(9, 45)
    assert bucket_start(ts(10, 14), 3600) == ts(9, 15)
    assert bucket_start(ts(10, 16), 3600) == ts(10, 15)
    assert bucket_start(ts(9, 17, 3), 5) == ts(9, 17, 0)
    assert bucket_start(ts(14, 0), 86400) == ts(0, 0)


def test_aggregator_builds_all_frames_and_volume_deltas():
    a = SeriesAggregator("X")
    a.on_tick(ts(9, 15, 0), 100, cum_vol=1000)
    a.on_tick(ts(9, 15, 3), 102, cum_vol=1300)
    out = a.on_tick(ts(9, 15, 6), 99, cum_vol=1350)
    frames = {tf for tf, _, _ in out}
    assert {"1s", "5s", "1m", "1d"} <= frames
    m1 = a.get("1m")[-1]
    assert (m1["o"], m1["h"], m1["l"], m1["c"], m1["v"]) == (100, 102, 99, 99, 350)
    assert len(a.get("5s")) == 2
    assert a.on_tick(ts(9, 15, 7), 0) == []  # zero ticks never enter candles


def test_backfill_goes_in_front_never_over_live():
    a = SeriesAggregator("X")
    a.on_tick(ts(9, 20), 105)
    n = a.backfill("1m", [{"t": int(ts(9, 18)), "o": 1, "h": 2, "l": 1, "c": 2}, {"t": int(ts(9, 20)), "o": 9, "h": 9, "l": 9, "c": 9}])
    assert n == 1
    bars = a.get("1m")
    assert [b["t"] for b in bars] == [int(ts(9, 18)), int(ts(9, 20))]
    assert bars[-1]["c"] == 105


def test_stitch_prefers_live_bars():
    hist = from_fyers_history({"candles": [[100, 1, 2, 1, 2, 0], [160, 2, 3, 2, 3, 0], [220, 3, 4, 3, 4, 0]]})
    live = [{"t": 160, "o": 5, "h": 6, "l": 5, "c": 6, "v": 0}]
    out = stitch(hist, live)
    assert [c["t"] for c in out] == [100, 160]
    assert out[-1]["c"] == 6


def test_chain_column_contract():
    assert COLUMN_LABELS == ["OPEN", "HIGH", "LOW", "CLOSE", "BID", "ASK", "MARK PRICE (CMP)", "VOLUME"]
    t = {"open": 1, "high": 2, "low": 0.5, "prev_close": 1.2, "bid": 1.1, "ask": 1.15, "ltp": 1.12, "vol": 900}
    assert list(cells_from_tick(t)) == COLUMNS
    assert cells_from_tick(t)["close"] == 1.2 and cells_from_tick(t)["mark"] == 1.12


def test_parse_fyers_chain_v3_shape():
    payload = {"code": 200, "s": "ok", "data": {
        "expiryData": [{"date": "04-11-2026", "expiry": "1793786400"}],
        "optionsChain": [
            {"symbol": "NSE:NIFTY50-INDEX", "strike_price": -1, "option_type": "", "ltp": 24321.5},
            {"symbol": "NSE:NIFTY26N0424300CE", "strike_price": 24300, "option_type": "CE"},
            {"symbol": "NSE:NIFTY26N0424300PE", "strike_price": 24300, "option_type": "PE"},
            {"symbol": "NSE:NIFTY26N0424350CE", "strike_price": 24350, "option_type": "CE"},
        ]}}
    c = parse_fyers_chain(payload)
    assert c["spot"] == 24321.5
    assert c["expiries"] == [{"date": "2026-11-04", "ts": "1793786400"}]
    assert c["rows"][0] == {"strike": 24300.0, "CE": "NSE:NIFTY26N0424300CE", "PE": "NSE:NIFTY26N0424300PE"}


def test_symbol_helpers():
    assert instrument_for_symbol("NSE:NIFTYNXT5026N0467300CE") == "NIFTYNXT50"
    assert instrument_for_symbol("NSE:BANKNIFTY26NOV52800PE") == "BANKNIFTY"
    assert instrument_for_symbol("NSE:NIFTY26N0424300CE") == "NIFTY50"
    assert instrument_for_symbol("BSE:SENSEX-INDEX") == "SENSEX"
    assert atm_strike("BANKNIFTY", 52849) == 52800
    from datetime import date
    s = option_symbol("SENSEX", date(2026, 11, 5), 80100, "PE")
    assert parse_option_symbol(s) == ("SENSEX", date(2026, 11, 5), 80100.0, "PE")
