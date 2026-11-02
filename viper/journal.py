"""Journal queries and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from viper.market.candles import IST
from viper.market.instruments import INSTRUMENTS
from viper.store.db import DB

CSV_COLUMNS = [
    "trade_id", "mode", "instrument", "symbol", "strike", "option_type", "side", "qty", "lots",
    "entry_time_ist", "exit_time_ist", "entry_price", "exit_price", "gross_pnl", "fees", "net_pnl",
    "hold_seconds", "strategy", "regime", "exit_reason", "mfe_pts", "mae_pts", "slippage_pts", "origin", "spot_at_entry",
]


def _day_bounds(d: date) -> tuple[float, float]:
    start = datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()
    return start, start + 86400


def build_filter(mode: str = "both", date_mode: str = "all", day: str | None = None, start: str | None = None,
                 end: str | None = None, instrument: str = "ALL") -> tuple[str, list]:
    where, args = ["status IN ('closed','open')"], []
    if mode in ("live", "shadow"):
        where.append("mode=?")
        args.append(mode)
    elif mode != "both":
        raise ValueError("mode must be live, shadow or both")
    if date_mode == "today":
        lo, hi = _day_bounds(datetime.now(IST).date())
        where.append("entry_ts>=? AND entry_ts<?")
        args += [lo, hi]
    elif date_mode == "date":
        if not day:
            raise ValueError("date required")
        lo, hi = _day_bounds(date.fromisoformat(day))
        where.append("entry_ts>=? AND entry_ts<?")
        args += [lo, hi]
    elif date_mode == "range":
        if not start or not end:
            raise ValueError("start and end required")
        lo = _day_bounds(date.fromisoformat(start))[0]
        hi = _day_bounds(date.fromisoformat(end))[1]
        if hi <= lo:
            raise ValueError("end must be on/after start")
        where.append("entry_ts>=? AND entry_ts<?")
        args += [lo, hi]
    elif date_mode != "all":
        raise ValueError("date_mode must be today, date, range or all")
    if instrument != "ALL":
        if instrument not in INSTRUMENTS:
            raise ValueError("unknown instrument")
        where.append("instrument=?")
        args.append(instrument)
    return " AND ".join(where), args


def query(db: DB, limit: int = 1000, **filters) -> list[dict]:
    w, a = build_filter(**filters)
    return db.q(f"SELECT * FROM trades WHERE {w} ORDER BY entry_ts DESC LIMIT ?", [*a, limit])


def _ist(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in sorted(rows, key=lambda r: r["entry_ts"]):
        hold = round(r["exit_ts"] - r["entry_ts"]) if r.get("exit_ts") else ""
        w.writerow([
            r["id"], r["mode"], r["instrument"], r["symbol"], r.get("strike") or "", r.get("option_type") or "",
            "BUY" if r["side"] == 1 else "SELL", r["qty"], r["lots"], _ist(r["entry_ts"]), _ist(r.get("exit_ts")),
            f"{r['entry_price']:.2f}", f"{r['exit_price']:.2f}" if r.get("exit_price") is not None else "",
            _n(r.get("gross_pnl")), _n(r.get("fees")), _n(r.get("net_pnl")), hold, r["strategy"], r.get("regime") or "",
            r.get("exit_reason") or "", _n(r.get("mfe")), _n(r.get("mae")), _n(r.get("slippage")), r.get("origin") or "",
            _n(r.get("spot_at_entry")),
        ])
    return buf.getvalue()


def _n(v) -> str:
    return "" if v is None else f"{v:.2f}"


def filename(mode: str, date_mode: str, day: str | None, start: str | None, end: str | None, instrument: str) -> str:
    when = {"today": datetime.now(IST).strftime("%Y%m%d"), "date": (day or "").replace("-", ""),
            "range": f"{(start or '').replace('-', '')}-{(end or '').replace('-', '')}", "all": "all"}[date_mode]
    return f"viper_{mode}_{instrument.lower()}_{when}.csv"


def stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed"]
    wins = [r for r in closed if (r["net_pnl"] or 0) > 0]
    tot = sum(r["net_pnl"] or 0 for r in closed)
    return {
        "trades": len(closed), "wins": len(wins), "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "net": round(tot, 2), "avg": round(tot / len(closed), 2) if closed else None,
        "avg_hold_s": round(sum((r["exit_ts"] - r["entry_ts"]) for r in closed) / len(closed)) if closed else None,
    }


