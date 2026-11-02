"""Orchestrator: features -> setups -> rules -> learning -> risk -> books.

Shadow always runs (every instrument, every setup that passes rules) because its
job is exploration. Live runs per instrument according to the automation matrix:

  OFF  : never touch the instrument.
  ON   : execute setups at fixed sizing (guardrails only), ATM-first strikes,
         heuristic confidence >= 0.60. No regime-driven switching.
  AUTO : learner-weighted selection (posterior mean blended with confidence),
         strategy x regime pairs with a poor 4-week record are switched off,
         strike moneyness chosen by regime, size scaled 0.5x..1.5x by edge.

Live orders additionally require: LIVE_TRADING_ENABLED=true, a live (non
synthetic) feed, an authenticated trading app, market hours, kill switch off,
and the daily loss limit not breached.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque

from viper.bus import bus
from viper.config import Settings
from viper.engine import clock
from viper.engine.book import LiveBook, ShadowBook
from viper.engine.features import Signal, compute, detect, regime
from viper.engine.learner import Learner
from viper.engine.risk import RiskConfig, size, strike_plan
from viper.learn import rules as rulebook
from viper.market.chain import chains, expiry_today
from viper.market.hub import hub
from viper.market.instruments import INSTRUMENTS, VIX_SYMBOL, atm_strike, lot_size
from viper.store.db import DB

log = logging.getLogger("viper.engine")

SHADOW_MAX_OPEN = 8
SHADOW_THRESHOLD = 0.50
ON_THRESHOLD = 0.60
AUTO_THRESHOLD = 0.55


class Engine:
    def __init__(self, db: DB, cfg: Settings):
        self.db = db
        self.cfg = cfg
        self.learner = Learner(db, cfg.learning_window_days)
        self.shadow = ShadowBook(db, self.learner, cfg.shadow_start_capital)
        self.live = LiveBook(db, self.learner)
        self.features: dict[str, dict] = {}
        self.regimes: dict[str, str] = {}
        self.decisions: deque = deque(maxlen=300)
        self.last_eval: dict[str, float] = {}
        self.engine_syms: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.alerts: deque = deque(maxlen=50)
        # Shadow trades from a previous process can't be managed (no ticks while down): void them.
        db.x("UPDATE trades SET status='void', exit_reason='process restart' WHERE mode='shadow' AND status='open'")
        self.live.restore()
        bus.on("tick", self._on_tick)
        bus.on("bar_close", self._on_bar)

    # ------------------------------------------------------------- config
    @property
    def risk(self) -> RiskConfig:
        return RiskConfig.from_dict(self.db.kv_get("risk_config"))

    def save_risk(self, patch: dict) -> RiskConfig:
        cur = self.risk.as_dict()
        if "automation" in patch:
            cur["automation"] = {**cur["automation"], **patch.pop("automation")}
        cur.update(patch)
        cfg = RiskConfig.from_dict(cur)
        self.db.kv_set("risk_config", cfg.as_dict())
        bus.publish("risk", cfg.as_dict())
        if cfg.kill_switch:
            self.live.kill()
        return cfg

    def scoped_rules(self, key: str) -> list[dict]:
        rows = self.db.q("SELECT * FROM rules WHERE enabled=1 AND enforced=1 AND scope IN (?, 'ALL')", (key,))
        for r in rows:
            r["params"] = json.loads(r["params"])
        return rows

    # ------------------------------------------------------------- events
    def _on_tick(self, t: dict) -> None:
        self.shadow.on_tick(t)
        self.live.on_tick(t)

    def _on_bar(self, ev: dict) -> None:
        key = next((k for k, i in INSTRUMENTS.items() if i.spot == ev["symbol"]), None)
        if not key or ev["tf"] not in ("5s", "1m"):
            return
        now = time.time()
        if now - self.last_eval.get(key, 0) < 4.5:
            return
        self.last_eval[key] = now
        try:
            self.evaluate(key)
        except Exception:
            log.exception("evaluate %s failed", key)

    # ------------------------------------------------------------ helpers
    def _ensure_option_subs(self, key: str, spot: float) -> None:
        try:
            s = chains.structure(key)
        except Exception as e:
            log.debug("chain unavailable for %s: %s", key, e)
            return
        atm = atm_strike(key, spot)
        step = INSTRUMENTS[key].strike_step
        want = {atm + i * step for i in range(-2, 3)}
        syms = [r[c] for r in s["rows"] if r["strike"] in want for c in ("CE", "PE") if c in r]
        if sorted(syms) != sorted(self.engine_syms.get(key, [])):
            hub.release(f"engine:{key}")
            hub.subscribe(f"engine:{key}", syms)
            self.engine_syms[key] = syms

    def _ofi(self, key: str, spot: float) -> float | None:
        p_ce = chains.pick(key, 1, 0)
        p_pe = chains.pick(key, -1, 0)
        if not p_ce or not p_pe:
            return None
        ce, pe = hub.quote(p_ce["symbol"]) or {}, hub.quote(p_pe["symbol"]) or {}

        def imb(q: dict) -> float | None:
            b, s = q.get("tbq"), q.get("tsq")
            return (b - s) / (b + s) if b and s and (b + s) > 0 else None

        a, b = imb(ce), imb(pe)
        if a is None or b is None:
            return None
        return (a - b) / 2

    def _record(self, **d) -> dict:
        d["ts"] = time.time()
        self.decisions.appendleft(d)
        bus.publish("decision", d, local=False)
        return d

    def live_blockers(self, key: str) -> list[str]:
        cfg = self.risk
        out = []
        mode = cfg.automation.get(key, "OFF")
        if mode == "OFF":
            out.append("automation OFF")
        if not self.cfg.live_trading_enabled:
            out.append("LIVE_TRADING_ENABLED=false")
        if hub.synthetic or hub.kind != "live":
            out.append("feed is not live")
        if not self.live.ready:
            out.append("trading API not authenticated")
        if not clock.market_open():
            out.append("market closed")
        if cfg.kill_switch:
            out.append("kill switch engaged")
        return out

    def deployable_equity(self) -> float:
        cfg = self.risk
        avail = self.live.available_margin()
        return min(cfg.equity_allocation, avail) if avail is not None else cfg.equity_allocation

    # ----------------------------------------------------------- decision
    def evaluate(self, key: str) -> None:
        inst = INSTRUMENTS[key]
        agg = hub.aggs[inst.spot]
        tick = hub.quote(inst.spot)
        if not tick:
            return
        self._ensure_option_subs(key, tick["ltp"])
        struct_expiry = None
        try:
            struct_expiry = chains.structure(key).get("expiry")
        except Exception:
            pass
        f = compute(key, agg.closed("1m", 150), agg.closed("5s", 80), tick, hub.ltp(VIX_SYMBOL),
                    self._ofi(key, tick["ltp"]), expiry_today(struct_expiry), clock.OPEN_HM, clock.CLOSE_HM)
        if not f:
            return
        rg = regime(f)
        self.features[key] = f.as_dict()
        self.regimes[key] = rg
        if f.minutes_to_close < 10 and not hub.synthetic:
            return  # no new entries into the close
        params = self.learner.params()
        sigs = detect(f, params)
        if not sigs:
            return
        rules = self.scoped_rules(key)
        cfg = self.risk
        caps = rulebook.overrides(rules)
        for sig in sigs:
            # max_trades_day is per book, so it is checked in each _decide with that book's count.
            ok, why = rulebook.check(rules, sig, f, 0, f.ts or time.time())
            if ok:
                need = params.get(sig.strategy, {}).get("min_resistance_atr", 0.0)
                room = ((f.day_high - f.price) if sig.direction > 0 else (f.price - f.day_low)) / f.atr if f.atr else 9
                if need and room < need:
                    ok, why = False, f"learned filter: only {room:.2f} ATR to day {'high' if sig.direction > 0 else 'low'} (< {need})"
            ctx = {"spot": f.price, "atr": f.atr, "regime": rg, "expiry_day": f.expiry_day, "mom_slope": f.mom_slope,
                   "resistance_atr": ((f.day_high - f.price) if sig.direction > 0 else (f.price - f.day_low)) / f.atr if f.atr else None,
                   "interval": "1m"}
            if not ok:
                self._record(key=key, strategy=sig.strategy, direction=sig.direction, regime=rg, confidence=sig.confidence,
                             action="blocked", reason=why)
                continue
            cap = min((r["params"]["value"] for r in rules if r["kind"] == "max_trades_day"), default=None)
            caps["_max_trades"] = cap
            self._shadow_decide(sig, f, cfg, caps, ctx)
            self._live_decide(sig, f, cfg, caps, ctx)

    def _hold_s(self, sig: Signal, cfg: RiskConfig, caps: dict) -> float:
        return 60 * min(sig.hold_min, cfg.max_hold_min, caps.get("max_hold_min", 1e9))

    def _pick_and_size(self, sig: Signal, f, cfg: RiskConfig, caps: dict, equity: float, auto: bool, mult: float):
        eff = RiskConfig.from_dict({**cfg.as_dict(), "max_sl_pct": min(cfg.max_sl_pct, caps.get("max_sl_pct", 1e9))})
        lot = lot_size(sig.key, self.cfg.lot_overrides, chains.lot_master)
        for off in strike_plan(sig.regime, auto):
            opt = chains.pick(sig.key, sig.direction, off)
            if not opt:
                continue
            q = hub.quote(opt["symbol"]) or {}
            prem = q.get("ask") or q.get("ltp")
            if not prem:
                continue
            sized = size(sig, prem, lot, equity, eff, f.atr, off, mult)
            if sized:
                return opt, sized
        return None, None

    def _shadow_decide(self, sig: Signal, f, cfg: RiskConfig, caps: dict, ctx: dict) -> None:
        sample = self.learner.sample(sig.strategy, sig.regime, sig.key)
        score = 0.5 * sample + 0.5 * sig.confidence
        base = dict(key=sig.key, strategy=sig.strategy, direction=sig.direction, regime=sig.regime,
                    confidence=round(sig.confidence, 3), score=round(score, 3), book="shadow", why=sig.reason)
        if score < SHADOW_THRESHOLD:
            self._record(**base, action="skip", reason=f"score {score:.2f} < {SHADOW_THRESHOLD}")
            return
        if any(t.row["strategy"] == sig.strategy for t in self.shadow.open_for(sig.key)):
            return
        if caps.get("_max_trades") is not None and self.shadow.trades_today() >= caps["_max_trades"]:
            return
        if len(self.shadow.open) >= SHADOW_MAX_OPEN or time.time() < self.shadow.cooldown_until.get(sig.key, 0):
            return
        equity = min(self.shadow.equity() - self.shadow.deployed(), cfg.equity_allocation)
        opt, sized = self._pick_and_size(sig, f, cfg, caps, equity, True, 1.0)
        if not sized:
            self._record(**base, action="skip", reason="no strike fits capital/SL budget")
            return
        row = self.shadow.enter(sig, opt, sized, ctx, self.learner.params().get(sig.strategy, {}).get("trail_at_r", 1.0),
                                self._hold_s(sig, cfg, caps))
        if row:
            self._record(**base, action="enter", reason=f"{opt['symbol']} x{sized.qty} @ {row['entry_price']}", trade_id=row["id"])

    def _live_decide(self, sig: Signal, f, cfg: RiskConfig, caps: dict, ctx: dict) -> None:
        mode = cfg.automation.get(sig.key, "OFF")
        if mode == "OFF":
            return
        blockers = self.live_blockers(sig.key)
        base = dict(key=sig.key, strategy=sig.strategy, direction=sig.direction, regime=sig.regime,
                    confidence=round(sig.confidence, 3), book="live", mode=mode, why=sig.reason)
        if blockers:
            self._record(**base, action="blocked", reason="; ".join(blockers))
            return
        auto = mode == "AUTO"
        if auto:
            mean = self.learner.mean(sig.strategy, sig.regime, sig.key)
            n = self.learner._ab(sig.strategy, sig.regime, sig.key)[2]
            if n >= 10 and mean < 0.4:
                self._record(**base, action="skip", reason=f"AUTO: {sig.strategy} in {sig.regime} win-rate {mean:.0%} over {n} trades")
                return
            score, thr, mult = 0.5 * mean + 0.5 * sig.confidence, AUTO_THRESHOLD, max(0.5, min(1.5, mean / 0.5))
        else:
            score, thr, mult = sig.confidence, ON_THRESHOLD, 1.0
        if score < thr:
            self._record(**base, action="skip", reason=f"score {score:.2f} < {thr}")
            return
        if self.live.open_for(sig.key) or len(self.live.open) >= cfg.max_open_live:
            return
        if caps.get("_max_trades") is not None and self.live.trades_today() >= caps["_max_trades"]:
            self._record(**base, action="blocked", reason=f"max {caps['_max_trades']} trades/day rule")
            return
        if time.time() < self.live.cooldown_until.get(sig.key, 0):
            return
        equity = self.deployable_equity()
        day = self.live.realized_today() + self.live.unrealized()
        if day <= -equity * cfg.daily_loss_pct / 100:
            self._record(**base, action="blocked", reason=f"daily loss limit hit ({day:.0f})")
            return
        opt, sized = self._pick_and_size(sig, f, cfg, caps, equity, auto, mult)
        if not sized:
            self._record(**base, action="skip", reason="no strike fits capital/SL budget")
            return
        row = self.live.enter(sig, opt, sized, ctx, self.learner.params().get(sig.strategy, {}).get("trail_at_r", 1.0),
                              self._hold_s(sig, cfg, caps))
        self._record(**base, score=round(score, 3), action="enter", reason=f"{opt['symbol']} x{sized.qty}", trade_id=row["id"])

    # ------------------------------------------------------ manual actions
    def manual_deploy(self, key: str, option_type: str, offset: int, lots: int, stop_pct: float, target_pct: float,
                      hold_min: float, symbol: str | None = None) -> dict:
        blockers = [b for b in self.live_blockers(key) if b != "automation OFF"]
        if blockers:
            raise PermissionError("; ".join(blockers))
        d = 1 if option_type == "CE" else -1
        opt = {"symbol": symbol, "strike": None, "option_type": option_type} if symbol else chains.pick(key, d, offset)
        if not opt:
            raise ValueError("no option found for that strike")
        q = hub.quote(opt["symbol"]) or {}
        prem = q.get("ask") or q.get("ltp")
        if not prem:
            hub.subscribe("manual", [opt["symbol"]])
            raise ValueError("no live quote yet for that option; retry in a second")
        lot = lot_size(key, self.cfg.lot_overrides, chains.lot_master)
        cfg = self.risk
        qty = lots * lot
        if prem * qty > self.deployable_equity() * cfg.max_capital_pct / 100:
            raise PermissionError(f"order value {prem * qty:,.0f} exceeds Max Capital Use {cfg.max_capital_pct}%")
        from viper.engine.risk import SizedOrder

        sized = SizedOrder(lots=lots, qty=qty, premium=prem, stop_premium=round(prem * (1 - stop_pct / 100), 2),
                           target_premium=round(prem * (1 + target_pct / 100), 2), capital=prem * qty,
                           risk=prem * qty * stop_pct / 100, strike_offset=offset, note="manual deploy")
        sig = Signal(key, "manual", d, 1.0, 0, 0, hold_min, "manual deploy", self.regimes.get(key, ""))
        return self.live.enter(sig, opt, sized, {"spot": hub.ltp(INSTRUMENTS[key].spot)}, 1.0, hold_min * 60, origin="user")

    # ----------------------------------------------------- periodic loop
    def start(self) -> None:
        threading.Thread(target=self._loop, name="engine-loop", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_eq = last_rec = 0.0
        while not self._stop.wait(1.0):
            try:
                self.shadow.housekeeping()
                self.live.housekeeping()
                now = time.time()
                if not hub.synthetic and clock.market_open() and clock.minutes_to_close() < 5:
                    for book in (self.shadow, self.live):
                        for tid, ot in list(book.open.items()):
                            if ot.row.get("origin") != "manual":
                                book.request_exit(tid, "eod")
                if now - last_rec >= 30:
                    last_rec = now
                    self.live.reconcile()
                if now - last_eq >= 60:
                    last_eq = now
                    self.db.insert("equity", {"ts": now, "mode": "shadow", "equity": self.shadow.equity(),
                                              "realized": self.shadow.realized_today(), "unrealized": self.shadow.unrealized()})
                    if self.live.ready:
                        self.db.insert("equity", {"ts": now, "mode": "live", "equity": self.live.funds_cache.get("available") or 0,
                                                  "realized": self.live.realized_today(), "unrealized": self.live.unrealized()})
                bus.publish("books", {
                    "shadow": {"open": self.shadow.snapshot(), "equity": self.shadow.equity(),
                               "realized": self.shadow.realized_today(), "unrealized": self.shadow.unrealized()},
                    "live": {"open": self.live.snapshot(), "realized": self.live.realized_today(),
                             "unrealized": self.live.unrealized(), "available": self.live.funds_cache.get("available")},
                    "regimes": self.regimes,
                }, local=False)
            except Exception:
                log.exception("engine loop")
