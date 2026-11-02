"""Trade lifecycle for Shadow and Live.

Both books share trade management (MFE/MAE tracking, stop/target/trail/time exits,
post-exit drift watch for the audit). They differ only in how fills happen:

* ``ShadowBook`` fills against the live bid/ask with a slippage model and charges.
* ``LiveBook`` sends orders to FYERS (trading app, trading IP), keeps a protective
  SL-M order at the broker, trails it with modify calls, and reconciles anything
  done by hand in the FYERS app via the order socket and a REST safety sweep.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from viper.bus import bus
from viper.engine.risk import fees
from viper.market.hub import hub
from viper.market.instruments import INSTRUMENTS, instrument_for_symbol
from viper.store.db import DB

log = logging.getLogger("viper.book")
TICK = 0.05
POST_EXIT_WATCH_S = 300


def r2(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


@dataclass
class OpenTrade:
    row: dict
    stop: float
    target: float
    max_hold_s: float
    trail_at_r: float = 1.0
    best: float = 0.0
    worst: float = 0.0
    ctx: dict = field(default_factory=dict)
    exiting: bool = False


@dataclass
class Watch:
    trade: dict
    until: float
    best: float
    ctx: dict


class BaseBook:
    mode = "base"

    def __init__(self, db: DB, learner):
        self.db = db
        self.learner = learner
        self.open: dict[int, OpenTrade] = {}
        self.watches: list[Watch] = []
        self.lock = threading.RLock()
        self.cooldown_until: dict[str, float] = {}

    # ------------------------------------------------------------ queries
    def open_for(self, key: str) -> list[OpenTrade]:
        with self.lock:
            return [t for t in self.open.values() if t.row["instrument"] == key]

    def trades_today(self) -> int:
        from viper.engine.clock import day_start

        r = self.db.one("SELECT COUNT(*) AS n FROM trades WHERE mode=? AND entry_ts>=? AND origin='viper'", (self.mode, day_start()))
        return r["n"] if r else 0

    def realized_today(self) -> float:
        from viper.engine.clock import day_start

        r = self.db.one("SELECT COALESCE(SUM(net_pnl),0) AS p FROM trades WHERE mode=? AND status='closed' AND exit_ts>=?", (self.mode, day_start()))
        return r["p"] if r else 0.0

    def unrealized(self) -> float:
        tot = 0.0
        with self.lock:
            for t in self.open.values():
                q = hub.quote(t.row["symbol"]) or {}
                px = q.get("bid") or q.get("ltp")
                if px:
                    tot += (px - t.row["entry_price"]) * t.row["qty"]
        return round(tot, 2)

    def snapshot(self) -> list[dict]:
        out = []
        with self.lock:
            items = list(self.open.values())
        for t in items:
            q = hub.quote(t.row["symbol"]) or {}
            ltp = q.get("ltp")
            exit_px = q.get("bid") or ltp
            out.append({
                **{k: t.row.get(k) for k in ("id", "instrument", "symbol", "side", "qty", "lots", "entry_ts", "entry_price",
                                             "strategy", "regime", "strike", "option_type", "origin", "status")},
                "ltp": ltp, "bid": q.get("bid"), "ask": q.get("ask"),
                "stop": t.stop, "target": t.target, "max_hold_s": t.max_hold_s,
                "upnl": round((exit_px - t.row["entry_price"]) * t.row["qty"], 2) if exit_px else None,
                "held_s": round(time.time() - t.row["entry_ts"]),
                "mfe": round(t.best - t.row["entry_price"], 2), "mae": round(t.row["entry_price"] - t.worst, 2),
            })
        return out

    # ----------------------------------------------------------- lifecycle
    def _register(self, row: dict, stop: float, target: float, max_hold_s: float, trail_at_r: float, ctx: dict) -> OpenTrade:
        ot = OpenTrade(row=row, stop=stop, target=target, max_hold_s=max_hold_s, trail_at_r=trail_at_r,
                       best=row["entry_price"], worst=row["entry_price"], ctx=ctx)
        with self.lock:
            self.open[row["id"]] = ot
        hub.subscribe(f"{self.mode}:{row['id']}", [row["symbol"]])
        bus.publish(f"{self.mode}_open", self.public(row))
        return ot

    def public(self, row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "meta"}

    def on_tick(self, t: dict) -> None:
        sym = t["symbol"]
        now = time.time()
        with self.lock:
            targets = [ot for ot in self.open.values() if ot.row["symbol"] == sym and not ot.exiting]
            watches = [w for w in self.watches if w.trade["symbol"] == sym]
        exit_px = t.get("bid") or t.get("ltp")
        if exit_px:
            for w in watches:
                w.best = max(w.best, exit_px)
        for ot in targets:
            if not exit_px:
                continue
            entry = ot.row["entry_price"]
            ot.best = max(ot.best, exit_px)
            ot.worst = min(ot.worst, exit_px)
            if ot.row.get("origin") == "manual" or ot.row.get("stop_price") is None:
                continue  # adopted manual position: tracked, never auto-exited
            risk = max(entry - ot.row["stop_price"], TICK)
            # trail: breakeven at trail_at_r, then lock 50% of open profit
            if ot.best - entry >= ot.trail_at_r * risk:
                new_stop = r2(max(ot.stop, entry + 0.5 * (ot.best - entry), entry))
                if new_stop > ot.stop:
                    ot.stop = new_stop
                    self.on_trail(ot)
            reason = None
            if exit_px <= ot.stop:
                reason = "trail" if ot.stop >= entry else "stop"
            elif exit_px >= ot.target:
                reason = "target"
            elif now - ot.row["entry_ts"] >= ot.max_hold_s:
                reason = "time"
            if reason:
                self.request_exit(ot.row["id"], reason)

    def housekeeping(self) -> None:
        now = time.time()
        due = []
        with self.lock:
            for w in list(self.watches):
                if now >= w.until:
                    self.watches.remove(w)
                    due.append(w)
            timed = [ot.row["id"] for ot in self.open.values() if not ot.exiting and now - ot.row["entry_ts"] >= ot.max_hold_s]
        for tid in timed:
            self.request_exit(tid, "time")
        for w in due:
            hub.release(f"{self.mode}-watch:{w.trade['id']}")
            try:
                self.learner.audit(w.trade, post_exit_best=w.best, ctx=w.ctx)
            except Exception:
                log.exception("audit failed for trade %s", w.trade["id"])

    def finalize(self, tid: int, exit_price: float, reason: str, exit_slip: float = 0.0) -> dict | None:
        with self.lock:
            ot = self.open.pop(tid, None)
        if not ot:
            return None
        row = ot.row
        inst = INSTRUMENTS.get(row["instrument"])
        buy_val = row["entry_price"] * row["qty"]
        sell_val = exit_price * row["qty"]
        gross = round(sell_val - buy_val, 2)
        fee = fees(inst.exchange if inst else "NSE", buy_val, sell_val)
        upd = {
            "exit_ts": time.time(), "exit_price": round(exit_price, 2), "gross_pnl": gross, "fees": fee,
            "net_pnl": round(gross - fee, 2), "exit_reason": reason, "status": "closed",
            "mfe": round(ot.best - row["entry_price"], 2), "mae": round(row["entry_price"] - ot.worst, 2),
            "slippage": round((row.get("slippage") or 0) + exit_slip, 2),
        }
        self.db.update("trades", "id", tid, upd)
        row.update(upd)
        hub.release(f"{self.mode}:{tid}")
        if row.get("origin") == "viper":
            hub.subscribe(f"{self.mode}-watch:{tid}", [row["symbol"]])
            with self.lock:
                self.watches.append(Watch(trade=dict(row), until=time.time() + POST_EXIT_WATCH_S, best=exit_price, ctx=ot.ctx))
        if (row["net_pnl"] or 0) < 0:
            self.cooldown_until[row["instrument"]] = time.time() + 60
        bus.publish(f"{self.mode}_close", self.public(row))
        return row

    # hooks
    def on_trail(self, ot: OpenTrade) -> None:
        pass

    def request_exit(self, tid: int, reason: str) -> None:
        raise NotImplementedError


class ShadowBook(BaseBook):
    mode = "shadow"

    def __init__(self, db: DB, learner, start_capital: float):
        super().__init__(db, learner)
        self.start_capital = start_capital

    def equity(self) -> float:
        r = self.db.one("SELECT COALESCE(SUM(net_pnl),0) AS p FROM trades WHERE mode='shadow' AND status='closed'")
        return round(self.start_capital + (r["p"] if r else 0) + self.unrealized(), 2)

    def deployed(self) -> float:
        with self.lock:
            return sum(t.row["entry_price"] * t.row["qty"] for t in self.open.values())

    @staticmethod
    def slip_ticks(qty: int, spread: float) -> int:
        """1 tick base, +1 if the spread is wide, +1 per 1,800 units of size."""
        return 1 + (1 if spread > 4 * TICK else 0) + qty // 1800

    def enter(self, sig, opt: dict, sized, ctx: dict, trail_at_r: float, max_hold_s: float) -> dict | None:
        q = hub.quote(opt["symbol"]) or {}
        ask, bid, ltp = q.get("ask"), q.get("bid"), q.get("ltp")
        if not ltp:
            return None
        ref_ask = ask or ltp * 1.001
        spread = (ask - bid) if (ask and bid) else ltp * 0.002
        slip = self.slip_ticks(sized.qty, spread) * TICK
        fill = r2(ref_ask + slip)
        shift = fill - sized.premium
        row = {
            "mode": "shadow", "instrument": sig.key, "symbol": opt["symbol"], "side": 1,
            "strike": opt["strike"], "option_type": opt["option_type"], "spot_at_entry": ctx.get("spot"),
            "qty": sized.qty, "lots": sized.lots, "entry_ts": time.time(), "entry_price": fill,
            "stop_price": r2(sized.stop_premium + shift), "target_price": r2(sized.target_premium + shift),
            "max_hold_s": int(max_hold_s), "strategy": sig.strategy, "regime": sig.regime,
            "confidence": round(sig.confidence, 3), "status": "open", "slippage": round(fill - (ask or ltp), 2),
            "origin": "viper", "meta": json.dumps({"reason": sig.reason, "note": sized.note, "ctx": ctx}),
        }
        row["id"] = self.db.insert("trades", row)
        self._register(row, row["stop_price"], row["target_price"], max_hold_s, trail_at_r, ctx)
        return row

    def request_exit(self, tid: int, reason: str) -> None:
        with self.lock:
            ot = self.open.get(tid)
            if not ot or ot.exiting:
                return
            ot.exiting = True
        q = hub.quote(ot.row["symbol"]) or {}
        bid = q.get("bid") or q.get("ltp") or ot.row["entry_price"]
        spread = (q["ask"] - q["bid"]) if q.get("ask") and q.get("bid") else 0
        slip = self.slip_ticks(ot.row["qty"], spread) * TICK
        self.finalize(tid, max(TICK, r2(bid - slip)), reason, exit_slip=slip)


class LiveBook(BaseBook):
    """Real orders. Every call goes through the trading-role REST client."""

    mode = "live"
    STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED = 2, 1, 5

    def __init__(self, db: DB, learner):
        super().__init__(db, learner)
        self.rest = None
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-orders")
        self.pending: dict[str, dict] = {}   # broker order id -> {"trade_id", "purpose"}
        self.funds_cache: dict = {}
        self.last_reconcile = 0.0

    def attach(self, rest) -> None:
        self.rest = rest

    @property
    def ready(self) -> bool:
        return bool(self.rest and self.rest.authed)

    def available_margin(self) -> float | None:
        if not self.ready:
            return None
        try:
            f = self.rest.funds()
        except Exception as e:
            log.warning("funds unavailable: %s", e)
            return self.funds_cache.get("available")
        avail = None
        for row in f.get("fund_limit", []):
            if row.get("id") == 10 or str(row.get("title", "")).lower().startswith("available balance"):
                avail = float(row.get("equityAmount") or 0)
        self.funds_cache = {"available": avail, "ts": time.time(), "raw": f.get("fund_limit", [])}
        return avail

    # ------------------------------------------------------------- entries
    def enter(self, sig, opt: dict, sized, ctx: dict, trail_at_r: float, max_hold_s: float, origin: str = "viper") -> dict:
        q = hub.quote(opt["symbol"]) or {}
        ask = q.get("ask") or q.get("ltp") or sized.premium
        limit = r2(ask + 3 * TICK)  # marketable limit: fills at the touch, caps runaway slippage
        row = {
            "mode": "live", "instrument": sig.key, "symbol": opt["symbol"], "side": 1,
            "strike": opt.get("strike"), "option_type": opt.get("option_type"), "spot_at_entry": ctx.get("spot"),
            "qty": sized.qty, "lots": sized.lots, "entry_ts": time.time(), "entry_price": ask,
            "stop_price": sized.stop_premium, "target_price": sized.target_premium, "max_hold_s": int(max_hold_s),
            "strategy": sig.strategy, "regime": sig.regime, "confidence": round(sig.confidence, 3),
            "status": "pending", "origin": origin,
            "meta": json.dumps({"reason": sig.reason, "note": sized.note, "ctx": ctx, "limit": limit,
                                "trail_at_r": trail_at_r}),
        }
        row["id"] = self.db.insert("trades", row)
        bus.publish("live_pending", self.public(row))
        self.pool.submit(self._place_entry, row, limit, trail_at_r, max_hold_s, ctx, ask)
        return row

    def _place_entry(self, row: dict, limit: float, trail_at_r: float, max_hold_s: float, ctx: dict, ref: float) -> None:
        order = {
            "symbol": row["symbol"], "qty": row["qty"], "type": 1, "side": 1, "productType": "INTRADAY",
            "limitPrice": limit, "stopPrice": 0, "validity": "IOC", "disclosedQty": 0,
            "offlineOrder": False, "orderTag": f"viper{row['id']}",
        }
        try:
            res = self.rest.place_order(order)
            oid = str(res.get("id"))
            self.db.update("trades", "id", row["id"], {"broker_order_id": oid})
            self.pending[oid] = {"trade_id": row["id"], "purpose": "entry", "row": row, "ref": ref,
                                 "trail_at_r": trail_at_r, "max_hold_s": max_hold_s, "ctx": ctx}
            bus.publish("order", {"trade_id": row["id"], "order_id": oid, "status": "sent", "symbol": row["symbol"]})
        except Exception as e:
            self.db.update("trades", "id", row["id"], {"status": "rejected", "exit_reason": f"order error: {e}"[:200]})
            bus.publish("order", {"trade_id": row["id"], "status": "rejected", "error": str(e)})
            log.error("entry order failed for trade %s: %s", row["id"], e)

    def _on_entry_fill(self, info: dict, price: float, qty: int) -> None:
        row = info["row"]
        shift = price - info["ref"]
        upd = {"entry_price": price, "qty": qty, "lots": max(1, qty // max(1, row["qty"] // max(1, row["lots"]))),
               "status": "open", "entry_ts": time.time(), "slippage": round(price - info["ref"], 2),
               "stop_price": r2(row["stop_price"] + shift), "target_price": r2(row["target_price"] + shift)}
        self.db.update("trades", "id", row["id"], upd)
        row.update(upd)
        ot = self._register(row, row["stop_price"], row["target_price"], info["max_hold_s"], info["trail_at_r"], info["ctx"])
        self.pool.submit(self._place_protective_stop, ot)

    def _place_protective_stop(self, ot: OpenTrade) -> None:
        order = {
            "symbol": ot.row["symbol"], "qty": ot.row["qty"], "type": 3, "side": -1, "productType": "INTRADAY",
            "limitPrice": 0, "stopPrice": ot.stop, "validity": "DAY", "disclosedQty": 0,
            "offlineOrder": False, "orderTag": f"viperSL{ot.row['id']}",
        }
        try:
            res = self.rest.place_order(order)
            oid = str(res.get("id"))
            ot.ctx["sl_order_id"] = oid
            self.pending[oid] = {"trade_id": ot.row["id"], "purpose": "stop"}
        except Exception as e:
            log.error("protective SL for trade %s failed (Viper still manages the stop on ticks): %s", ot.row["id"], e)

    def on_trail(self, ot: OpenTrade) -> None:
        oid = ot.ctx.get("sl_order_id")
        if oid and self.ready:
            self.pool.submit(self._modify_sl, oid, ot.stop)

    def _modify_sl(self, oid: str, stop: float) -> None:
        try:
            self.rest.modify_order({"id": oid, "type": 3, "stopPrice": stop})
        except Exception as e:
            log.warning("trail modify %s failed: %s", oid, e)

    def modify(self, tid: int, stop: float | None, target: float | None) -> dict:
        with self.lock:
            ot = self.open.get(tid)
            if not ot:
                raise KeyError(tid)
            if stop is not None:
                ot.stop = r2(stop)
            if target is not None:
                ot.target = r2(target)
        self.db.update("trades", "id", tid, {k: v for k, v in {"stop_price": ot.stop if stop is not None else None,
                                                                 "target_price": ot.target if target is not None else None}.items() if v is not None})
        if stop is not None:
            self.on_trail(ot)
        return {"id": tid, "stop": ot.stop, "target": ot.target}

    # --------------------------------------------------------------- exits
    def request_exit(self, tid: int, reason: str) -> None:
        with self.lock:
            ot = self.open.get(tid)
            if not ot or ot.exiting:
                return
            ot.exiting = True
        self.pool.submit(self._place_exit, ot, reason)

    def _place_exit(self, ot: OpenTrade, reason: str) -> None:
        sl = ot.ctx.get("sl_order_id")
        if sl:
            try:
                self.rest.cancel_order(sl)
            except Exception as e:
                log.info("cancel SL %s: %s (may already be filled)", sl, e)
        q = hub.quote(ot.row["symbol"]) or {}
        order = {
            "symbol": ot.row["symbol"], "qty": ot.row["qty"], "type": 2, "side": -1, "productType": "INTRADAY",
            "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0,
            "offlineOrder": False, "orderTag": f"viperX{ot.row['id']}",
        }
        try:
            res = self.rest.place_order(order)
            oid = str(res.get("id"))
            self.pending[oid] = {"trade_id": ot.row["id"], "purpose": "exit", "reason": reason,
                                 "ref": q.get("bid") or q.get("ltp")}
        except Exception as e:
            ot.exiting = False  # allow retry from the next tick / user action
            bus.publish("order", {"trade_id": ot.row["id"], "status": "exit_failed", "error": str(e)})
            log.error("exit order for trade %s failed: %s", ot.row["id"], e)

    # ------------------------------------------------- order socket events
    def on_order_event(self, o: dict) -> None:
        oid = str(o.get("id", ""))
        info = self.pending.get(oid)
        status = o.get("status")
        bus.publish("order", {"order_id": oid, "status": status, "symbol": o.get("symbol"),
                              "filled": o.get("filledQty"), "price": o.get("tradedPrice"), "msg": o.get("message")})
        if not info:
            return
        if status == self.STATUS_FILLED or (status in (self.STATUS_CANCELLED,) and (o.get("filledQty") or 0) > 0):
            self.pending.pop(oid, None)
            price = float(o.get("tradedPrice") or 0)
            qty = int(o.get("filledQty") or o.get("qty") or 0)
            if info["purpose"] == "entry":
                self._on_entry_fill(info, price, qty)
            elif info["purpose"] == "exit":
                ref = info.get("ref") or price
                self.finalize(info["trade_id"], price, info["reason"], exit_slip=max(0.0, (ref or price) - price))
            elif info["purpose"] == "stop":
                self.finalize(info["trade_id"], price, "stop")
        elif status in (self.STATUS_CANCELLED, self.STATUS_REJECTED, 7):
            self.pending.pop(oid, None)
            if info["purpose"] == "entry":
                self.db.update("trades", "id", info["trade_id"], {"status": "cancelled", "exit_reason": o.get("message") or "not filled"})
                bus.publish("live_cancel", {"id": info["trade_id"]})
            elif info["purpose"] == "exit":
                with self.lock:
                    ot = self.open.get(info["trade_id"])
                    if ot:
                        ot.exiting = False

    def on_position_event(self, p: dict) -> None:
        self.reconcile_positions([p])

    def reconcile(self) -> None:
        """REST safety sweep (every 30 s while authed): catches anything the socket missed."""
        if not self.ready:
            return
        self.last_reconcile = time.time()
        try:
            res = self.rest.positions()
        except Exception as e:
            log.warning("position reconcile failed: %s", e)
            return
        self.reconcile_positions(res.get("netPositions", []))

    def reconcile_positions(self, positions: list[dict]) -> None:
        by_sym: dict[str, dict] = {p.get("symbol"): p for p in positions if p.get("symbol")}
        with self.lock:
            open_syms = {ot.row["symbol"]: ot for ot in self.open.values()}
            pend_syms = {i["row"]["symbol"] for i in self.pending.values() if i.get("purpose") == "entry"}
        # closed in the FYERS app by hand
        for sym, ot in open_syms.items():
            p = by_sym.get(sym)
            if p is not None and int(p.get("netQty", 0)) == 0 and not ot.exiting:
                price = float(p.get("sellAvg") or hub.ltp(sym) or ot.row["entry_price"])
                log.info("trade %s closed outside Viper (FYERS app)", ot.row["id"])
                self.finalize(ot.row["id"], price, "manual_fyers")
            elif p is not None and int(p.get("netQty", 0)) > 0 and int(p["netQty"]) != ot.row["qty"]:
                ot.row["qty"] = int(p["netQty"])  # partial manual exit
                self.db.update("trades", "id", ot.row["id"], {"qty": ot.row["qty"]})
        # opened in the FYERS app by hand: adopt so it shows and can be managed
        for sym, p in by_sym.items():
            nq = int(p.get("netQty", 0))
            if nq > 0 and sym not in open_syms and sym not in pend_syms and (p.get("productType") in (None, "INTRADAY", "MARGIN")):
                key = instrument_for_symbol(sym)
                if not key:
                    continue
                row = {
                    "mode": "live", "instrument": key, "symbol": sym, "side": 1, "qty": nq,
                    "lots": 1, "entry_ts": time.time(), "entry_price": float(p.get("buyAvg") or p.get("netAvg") or 0),
                    "stop_price": None, "target_price": None, "max_hold_s": None, "strategy": "manual",
                    "regime": None, "status": "open", "origin": "manual", "meta": json.dumps({"adopted": True}),
                }
                row["id"] = self.db.insert("trades", row)
                self._register(row, 0.0, float("inf"), float("inf"), 99.0, {})
                log.info("adopted manual FYERS position %s x%s @ %s", sym, nq, row["entry_price"])

    def kill(self) -> None:
        """Emergency: square off everything at the broker, cancel Viper's stops."""
        with self.lock:
            ids = list(self.open)
            for ot in self.open.values():
                ot.exiting = True
        if self.ready:
            try:
                self.rest.exit_position(None)
            except Exception as e:
                log.error("exit_all failed: %s", e)
        for tid in ids:
            with self.lock:
                ot = self.open.get(tid)
            if ot:
                px = (hub.quote(ot.row["symbol"]) or {}).get("bid") or ot.row["entry_price"]
                self.finalize(tid, px, "kill_switch")

    def restore(self) -> None:
        """Reload open live trades after a restart (stops/targets from DB)."""
        for row in self.db.q("SELECT * FROM trades WHERE mode='live' AND status='open'"):
            meta = json.loads(row.get("meta") or "{}")
            self._register(row, row["stop_price"] or 0.0, row["target_price"] or float("inf"),
                           row["max_hold_s"] or float("inf"), meta.get("trail_at_r", 1.0), meta.get("ctx", {}))
