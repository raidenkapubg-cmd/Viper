"""Post-trade learning shared by Shadow and Live.

1. **Rolling 4-week posteriors.** For each (strategy, regime, instrument) the win
   probability is a Beta(alpha, beta) posterior rebuilt from closed trades inside
   ``LEARNING_WINDOW_DAYS``, with live trades counted double (real fills beat
   simulated ones). ``weight`` = posterior mean / 0.5, so 1.0 is neutral.
2. **Exploration.** Shadow draws a Thompson sample from the posterior, so untested
   or under-sampled setups still get traded on paper. Live uses the posterior
   mean, which is conservative.
3. **Audit.** Every closed trade is checked for concrete errors using MFE/MAE,
   slippage, hold time and post-exit drift. Each finding writes a ledger row
   (mistake, where it happened, patch) and applies a bounded parameter patch that
   ``features.detect`` reads on the next bar.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime

from viper.bus import bus
from viper.engine.features import REGIMES, STRATEGIES
from viper.market.candles import IST
from viper.store.db import DB

log = logging.getLogger("viper.learner")

# Bounds every automatic patch must stay inside.
PARAM_BOUNDS = {
    "stop_atr": (0.3, 1.5), "target_atr": (0.6, 3.0), "hold_min": (2, 45),
    "z": (1.0, 3.0), "min_ofi": (0.1, 0.5), "max_retest_atr": (0.2, 0.8), "stretch_atr": (1.0, 3.0),
    "trail_at_r": (0.5, 1.5), "min_resistance_atr": (0.0, 1.0),
}
DEFAULTS = {"trail_at_r": 1.0, "min_resistance_atr": 0.0}


class Learner:
    def __init__(self, db: DB, window_days: int):
        self.db = db
        self.window_s = window_days * 86400
        self.rng = random.Random()
        self.lock = threading.Lock()
        self.post: dict[tuple[str, str, str], tuple[float, float, int, float]] = {}
        self.rebuild()

    # ---------------------------------------------------------- posteriors
    def rebuild(self) -> None:
        since = time.time() - self.window_s
        rows = self.db.q(
            "SELECT strategy, regime, instrument, mode, net_pnl FROM trades WHERE status='closed' AND exit_ts>=? AND origin='viper'",
            (since,),
        )
        post: dict[tuple[str, str, str], list[float]] = {}
        for r in rows:
            k = (r["strategy"], r["regime"] or "chop", r["instrument"])
            a = post.setdefault(k, [1.0, 1.0, 0, 0.0])
            w = 2.0 if r["mode"] == "live" else 1.0
            if (r["net_pnl"] or 0) > 0:
                a[0] += w
            else:
                a[1] += w
            a[2] += 1
            a[3] += r["net_pnl"] or 0
        with self.lock:
            self.post = {k: tuple(v) for k, v in post.items()}  # type: ignore[misc]
        now = time.time()
        for (s, rg, inst), (a, b, n, pnl) in self.post.items():
            self.db.x(
                "INSERT INTO strategy_stats(strategy,regime,instrument,alpha,beta,weight,n,pnl,updated) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(strategy,regime,instrument) DO UPDATE SET alpha=excluded.alpha,beta=excluded.beta,"
                "weight=excluded.weight,n=excluded.n,pnl=excluded.pnl,updated=excluded.updated",
                (s, rg, inst, a, b, (a / (a + b)) / 0.5, n, pnl, now),
            )

    def _ab(self, strategy: str, regime: str, inst: str) -> tuple[float, float, int]:
        a, b, n, _ = self.post.get((strategy, regime, inst), (1.0, 1.0, 0, 0.0))
        return a, b, n

    def mean(self, strategy: str, regime: str, inst: str) -> float:
        a, b, _ = self._ab(strategy, regime, inst)
        return a / (a + b)

    def sample(self, strategy: str, regime: str, inst: str) -> float:
        a, b, _ = self._ab(strategy, regime, inst)
        return self.rng.betavariate(a, b)

    def weight(self, strategy: str, regime: str, inst: str) -> float:
        return self.mean(strategy, regime, inst) / 0.5

    def matrix(self) -> list[dict]:
        out = []
        for s in STRATEGIES:
            for rg in REGIMES:
                a = b = 1.0
                n = 0
                pnl = 0.0
                for (ss, rr, _), (aa, bb, nn, pp) in self.post.items():
                    if ss == s and rr == rg:
                        a += aa - 1
                        b += bb - 1
                        n += nn
                        pnl += pp
                out.append({"strategy": s, "regime": rg, "win_p": round(a / (a + b), 3), "n": n, "pnl": round(pnl, 2)})
        return out

    # -------------------------------------------------------------- params
    def params(self) -> dict:
        return self.db.kv_get("strategy_params", {}) or {}

    def patch(self, strategy: str, key: str, delta: float, default: float) -> tuple[float, float]:
        params = self.params()
        cur = params.get(strategy, {}).get(key, DEFAULTS.get(key, default))
        lo, hi = PARAM_BOUNDS.get(key, (-1e9, 1e9))
        new = round(min(hi, max(lo, cur + delta)), 4)
        params.setdefault(strategy, {})[key] = new
        self.db.kv_set("strategy_params", params)
        return cur, new

    # --------------------------------------------------------------- audit
    def audit(self, trade: dict, post_exit_best: float | None = None, ctx: dict | None = None) -> list[dict]:
        """Inspect a closed trade; write lessons and apply patches. Returns lessons."""
        ctx = ctx or {}
        lessons: list[dict] = []
        entry, exitp = trade["entry_price"], trade["exit_price"] or trade["entry_price"]
        risk_pts = max(entry - (trade["stop_price"] or entry * 0.8), 0.05)
        mfe_r = (trade["mfe"] or 0) / risk_pts
        mae_r = (trade["mae"] or 0) / risk_pts
        pnl = trade["net_pnl"] or 0
        held = (trade["exit_ts"] or time.time()) - trade["entry_ts"]
        strat, rg, inst = trade["strategy"], trade["regime"] or "chop", trade["instrument"]
        where = {
            "instrument": inst, "regime": rg, "interval": ctx.get("interval", "1m"),
            "ts": trade["entry_ts"], "trade_id": trade["id"], "mode": trade["mode"],
        }
        side = "Call" if trade.get("option_type") == "CE" else "Put"

        def add(kind: str, mistake: str, key: str | None = None, delta: float = 0.0, default: float = 0.0, why: str = ""):
            patch_txt, patch_json = "", None
            if key:
                old, new = self.patch(strat, key, delta, default)
                patch_txt = f"{strat}.{key}: {old} -> {new}" + (f" ({why})" if why else "")
                patch_json = {"strategy": strat, "param": key, "old": old, "new": new}
            row = {
                "ts": time.time(), "mode": trade["mode"], "trade_id": trade["id"], "instrument": inst,
                "regime": rg, "interval": where["interval"], "kind": kind, "mistake": mistake,
                "patch": patch_txt or "No parameter change: logged for pattern tracking",
                "patch_json": json.dumps(patch_json) if patch_json else None,
            }
            row["id"] = self.db.insert("lessons", row)
            lessons.append(row)

        reason = trade.get("exit_reason") or ""
        if pnl <= 0 and mfe_r >= 1.0:
            add("late_exit", f"Gave back a {mfe_r:.1f}R open profit on {inst} {side}; exited at {reason or 'loss'}",
                "trail_at_r", -0.1, 1.0, "trail activates earlier")
        if reason == "stop" and held < 60 and mae_r <= 1.3 and post_exit_best is not None and post_exit_best >= entry + risk_pts:
            add("stop_noise", f"Stopped out of {inst} {side} in {held:.0f}s, then price ran {((post_exit_best - entry) / risk_pts):.1f}R in favour",
                "stop_atr", +0.05, 0.6, "stop sits outside 1m noise")
        if reason in ("time", "trail", "target") and post_exit_best is not None and pnl > 0:
            extra_r = (post_exit_best - exitp) / risk_pts
            if extra_r >= 1.0:
                if reason == "time":
                    add("premature_exit", f"Time exit on {inst} {side} left {extra_r:.1f}R on the table",
                        "hold_min", +1.0, 5.0, "allow more time in this setup")
                else:
                    add("premature_exit", f"{reason.title()} exit on {inst} {side} left {extra_r:.1f}R on the table",
                        "target_atr", +0.1, 1.2, "wider target")
        if reason == "time" and pnl <= 0 and mfe_r < 0.3:
            msg = f"Held {inst} {side} {held / 60:.0f} min with no follow-through"
            if ctx.get("expiry_day"):
                msg = f"Held {inst} {side} past the max theta decay window on expiry day ({held / 60:.0f} min, MFE {mfe_r:.1f}R)"
            add("dead_trade", msg, "hold_min", -1.0, 5.0, "cut stagnant trades sooner")
        if pnl <= 0 and ctx.get("resistance_atr") is not None and ctx["resistance_atr"] < 0.25:
            lvl = "resistance" if trade.get("option_type") == "CE" else "support"
            fading = " with fading momentum" if (ctx.get("mom_slope") or 0) * (1 if side == "Call" else -1) < 0 else ""
            add("into_level", f"Entered {inst} {side} into {lvl} ({ctx['resistance_atr']:.2f} ATR away){fading}",
                "min_resistance_atr", +0.1, 0.0, f"require room to {lvl}")
        if (trade.get("slippage") or 0) > 0.15:  # > 3 ticks per unit, entry + exit combined
            add("slippage", f"Slippage {trade['slippage']:.2f} pts per unit on {trade['symbol']}: spread too wide for a scalp")
        if not lessons and pnl > 0 and mfe_r >= 1.5 and reason == "target":
            add("good_trade", f"Clean {mfe_r:.1f}R {strat} winner on {inst} in {rg}: reinforced (posterior updated)")

        for row in lessons:
            bus.publish("lesson", row)
        self.rebuild()
        return lessons

    def cycle_progress(self) -> dict:
        first = self.db.one("SELECT MIN(entry_ts) AS t FROM trades WHERE mode='shadow'")
        start = (first or {}).get("t") or time.time()
        days = (time.time() - start) / 86400
        span = self.window_s / 86400
        cyc = int(days // span)
        return {
            "started": datetime.fromtimestamp(start, IST).isoformat(),
            "day": round(days % span, 2), "cycle": cyc + 1, "span_days": span,
            "pct": round(100 * (days % span) / span, 1),
        }
