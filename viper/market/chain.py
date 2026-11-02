"""Option chain: structure from REST once, prices from the socket thereafter.

``options-chain-v3`` gives strikes, symbols and expiries but no OHLC, so the
chain is loaded once per (instrument, expiry), seeded with a single quotes call
(<=50 symbols per call) for OPEN/HIGH/LOW/CLOSE, and then every row updates from
the shared data socket. Opening the chain in five tabs costs the same as one.

Column contract (strict order, both sides):
    OPEN, HIGH, LOW, CLOSE, BID, ASK, MARK PRICE (CMP/LTP), VOLUME
CLOSE is the previous session close (what the exchange publishes as "close"
during market hours); MARK is the live LTP.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from datetime import datetime

from viper.market.candles import IST
from viper.market.hub import hub
from viper.market.instruments import INSTRUMENTS, atm_strike

log = logging.getLogger("viper.chain")

COLUMNS = ["open", "high", "low", "close", "bid", "ask", "mark", "volume"]
COLUMN_LABELS = ["OPEN", "HIGH", "LOW", "CLOSE", "BID", "ASK", "MARK PRICE (CMP)", "VOLUME"]


def cells_from_tick(t: dict | None) -> dict:
    t = t or {}
    return {
        "open": t.get("open"), "high": t.get("high"), "low": t.get("low"),
        "close": t.get("prev_close"), "bid": t.get("bid"), "ask": t.get("ask"),
        "mark": t.get("ltp"), "volume": t.get("vol"),
    }


def seed_from_quote(q: dict) -> dict:
    v = q.get("v", {})
    return {
        "symbol": q.get("n") or v.get("symbol"), "ltp": v.get("lp"),
        "open": v.get("open_price"), "high": v.get("high_price"), "low": v.get("low_price"),
        "prev_close": v.get("prev_close_price"), "bid": v.get("bid"), "ask": v.get("ask"),
        "vol": v.get("volume"), "ts": v.get("tt") or time.time(),
    }


def parse_fyers_chain(payload: dict) -> dict:
    data = payload.get("data", payload)
    rows: dict[float, dict] = {}
    spot = None
    for o in data.get("optionsChain", []):
        ot = o.get("option_type")
        if ot in ("CE", "PE"):
            k = float(o["strike_price"])
            rows.setdefault(k, {"strike": k})[ot] = o["symbol"]
        elif o.get("strike_price", -1) == -1:
            spot = o.get("ltp")
    exps = []
    for e in data.get("expiryData", []):
        try:
            d = datetime.strptime(e["date"], "%d-%m-%Y").date().isoformat()
        except (KeyError, ValueError):
            d = e.get("date")
        exps.append({"date": d, "ts": str(e.get("expiry", ""))})
    return {"expiries": exps, "spot": spot, "rows": [rows[k] for k in sorted(rows)]}


class ChainService:
    def __init__(self) -> None:
        self.rest = None          # data-role FyersRest (live mode)
        self.synthetic = None     # SyntheticFeed (staging)
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.watch: dict[str, list[str]] = {}  # client -> symbols
        self.lot_master: dict[str, int] = {}
        self.lock = threading.Lock()

    def configure(self, rest, synthetic) -> None:
        self.rest, self.synthetic = rest, synthetic
        self.cache.clear()

    def structure(self, key: str, expiry_ts: str = "", strikecount: int = 10) -> dict:
        ck = (key, expiry_ts, strikecount)
        hit = self.cache.get(ck)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        inst = INSTRUMENTS[key]
        if self.synthetic is not None:
            exp = None
            if expiry_ts:
                exp = datetime.fromtimestamp(int(expiry_ts), IST).date()
            out = self.synthetic.option_chain(key, strikecount, exp)
            for e in out["expiries"]:
                if e["date"] == out["expiry"]:
                    out["expiry_ts"] = e["ts"]
        elif self.rest is not None:
            out = parse_fyers_chain(self.rest.option_chain(inst.spot, strikecount, expiry_ts))
            out["expiry_ts"] = expiry_ts or (out["expiries"][0]["ts"] if out["expiries"] else "")
            out["expiry"] = next((e["date"] for e in out["expiries"] if e["ts"] == out["expiry_ts"]), None)
            syms = [r[s] for r in out["rows"] for s in ("CE", "PE") if s in r]
            try:
                for q in self.rest.quotes(syms).get("d", []):
                    t = seed_from_quote(q)
                    if t["symbol"] and t["symbol"] not in hub.last:
                        hub.last[t["symbol"]] = t
            except Exception as e:  # chain still works from socket ticks
                log.warning("chain quote seed failed: %s", e)
        else:
            raise RuntimeError("no market data source (connect the data API or enable synthetic feed)")
        out["key"] = key
        with self.lock:
            self.cache[ck] = (time.time(), out)
        return out

    def snapshot(self, key: str, expiry_ts: str = "", strikecount: int = 10) -> dict:
        s = self.structure(key, expiry_ts, strikecount)
        spot = hub.ltp(INSTRUMENTS[key].spot) or s.get("spot")
        atm = atm_strike(key, spot) if spot else None
        rows = []
        for r in s["rows"]:
            rows.append({
                "strike": r["strike"], "atm": r["strike"] == atm,
                "CE": {"symbol": r.get("CE"), **cells_from_tick(hub.quote(r.get("CE", "")))},
                "PE": {"symbol": r.get("PE"), **cells_from_tick(hub.quote(r.get("PE", "")))},
            })
        return {
            "key": key, "spot": spot, "atm": atm, "expiry": s.get("expiry"), "expiry_ts": s.get("expiry_ts"),
            "expiries": s["expiries"], "columns": COLUMNS, "labels": COLUMN_LABELS, "rows": rows,
        }

    def watch_client(self, client: str, key: str, expiry_ts: str = "") -> dict:
        snap = self.snapshot(key, expiry_ts)
        syms = [row[s]["symbol"] for row in snap["rows"] for s in ("CE", "PE") if row[s]["symbol"]]
        owner = f"chain:{client}"
        hub.release(owner)
        hub.subscribe(owner, syms)
        self.watch[client] = syms
        return snap

    def unwatch_client(self, client: str) -> None:
        hub.release(f"chain:{client}")
        self.watch.pop(client, None)

    def pick(self, key: str, direction: int, offset_steps: int = 0) -> dict | None:
        """Pick nearest-expiry option. direction +1 -> CE, -1 -> PE.
        offset_steps > 0 = OTM, < 0 = ITM."""
        s = self.structure(key)
        spot = hub.ltp(INSTRUMENTS[key].spot) or s.get("spot")
        if not spot or not s["rows"]:
            return None
        step = INSTRUMENTS[key].strike_step
        cp = "CE" if direction > 0 else "PE"
        target = atm_strike(key, spot) + (offset_steps * step if direction > 0 else -offset_steps * step)
        row = min(s["rows"], key=lambda r: abs(r["strike"] - target))
        sym = row.get(cp)
        if not sym:
            return None
        return {"symbol": sym, "strike": row["strike"], "option_type": cp, "expiry": s.get("expiry")}

    def load_lot_master(self, session) -> None:
        """Lot sizes from the public Fyers symbol master (falls back to config)."""
        urls = {"NSE": "https://public.fyers.in/sym_details/NSE_FO.csv", "BSE": "https://public.fyers.in/sym_details/BSE_FO.csv"}
        found: dict[str, int] = {}
        for exch, url in urls.items():
            try:
                r = session.get(url, timeout=20)
                r.raise_for_status()
                for row in csv.reader(io.StringIO(r.text)):
                    if len(row) < 14:
                        continue
                    ticker, underlying = row[9], row[13]
                    for key, inst in INSTRUMENTS.items():
                        if key in found or inst.exchange != exch:
                            continue
                        root = inst.fo_prefix.split(":")[1]
                        if underlying == root and ticker.endswith(("CE", "PE")):
                            found[key] = int(float(row[3]))
            except Exception as e:
                log.warning("lot master %s unavailable: %s", exch, e)
        self.lot_master = found


def expiry_today(expiry_iso: str | None) -> bool:
    return bool(expiry_iso) and expiry_iso == datetime.now(IST).date().isoformat()


chains = ChainService()
