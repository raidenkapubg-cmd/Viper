"""Viper HTTP + WebSocket server.

Run:  python -m viper   (reads .env; see .env.example)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from viper import journal
from viper.bus import bus
from viper.config import ROOT, settings
from viper.engine.engine import Engine
from viper.fyers import auth as fauth
from viper.fyers.rest import FyersError, FyersRest, NotAuthenticated
from viper.learn.ingest import ingest
from viper.market.candles import FYERS_RESOLUTION, IST, TIMEFRAMES, from_fyers_history, sanitize, stitch
from viper.market.chain import chains
from viper.market.hub import LiveDataFeed, LiveOrderFeed, hub
from viper.market.instruments import INSTRUMENTS, VIX_SYMBOL
from viper.market.synthetic import SyntheticFeed
from viper.net import binding
from viper.store.db import DB

log = logging.getLogger("viper")
WEB = ROOT / "web"


class State:
    db: DB
    engine: Engine
    rest: dict[str, FyersRest]
    routes: dict[str, str | None]
    binding_error: str = ""
    synthetic: SyntheticFeed | None = None
    started = time.time()


S = State()


# ------------------------------------------------------------------ wiring
def role_ip(role: str) -> str | None:
    return S.routes.get(role)


def data_client() -> tuple[FyersRest | None, str]:
    """The REST client used for market data. Falls back to the trading app when only one app exists."""
    d = S.rest["data"]
    if d.authed:
        return d, "data"
    if settings.data_uses_trading_token and S.rest["trading"].authed:
        return S.rest["trading"], "trading"
    return None, ""


def start_feeds() -> None:
    """(Re)build data + order feeds from current tokens. Safe to call repeatedly."""
    dc, via = data_client()
    want_live = settings.feed_mode in ("live", "auto") and dc is not None
    if hub.order_feed:
        hub.order_feed.stop()
        hub.order_feed = None
    if want_live:
        ip = role_ip(via)
        binding.set_ws_route("/hsm/", ip)
        feed = LiveDataFeed(hub.on_tick, dc.app_id, dc.access_token, ip, settings.data_dir / "logs")
        S.synthetic = None
        chains.configure(dc, None)
        hub.attach(feed)
        try:
            chains.load_lot_master(dc.session)
        except Exception as e:
            log.warning("lot master: %s", e)
    elif settings.feed_mode in ("synthetic", "auto"):
        S.synthetic = SyntheticFeed(hub.on_tick)
        chains.configure(None, S.synthetic)
        hub.attach(S.synthetic)
    else:
        chains.configure(None, None)
        log.warning("FEED_MODE=live but no data token: market feed offline until you connect the API")
    tr = S.rest["trading"]
    S.engine.live.attach(tr if tr.authed else None)
    if tr.authed:
        hub.order_feed = LiveOrderFeed(tr.app_id, tr.access_token, settings.data_dir / "logs", {
            "orders": S.engine.live.on_order_event,
            "positions": S.engine.live.on_position_event,
            "trades": lambda m: bus.publish("fill", m, local=False),
        })
        try:
            hub.order_feed.start()
        except Exception as e:
            log.error("order socket failed to start: %s", e)
    threading.Thread(target=backfill_core, name="backfill", daemon=True).start()
    bus.publish("network", network_status(), local=False)


def backfill_core() -> None:
    """Warm the engine: 1m and 5s history for every index so setups can fire immediately."""
    for key, inst in INSTRUMENTS.items():
        for tf, n in (("1m", 300), ("5s", 240)):
            try:
                if S.synthetic is not None:
                    hist = S.synthetic.history(key, TIMEFRAMES[tf], n, time.time() - TIMEFRAMES[tf])
                else:
                    dc, _ = data_client()
                    if dc is None:
                        return
                    now = int(time.time())
                    span = 86400 * 4 if tf == "1m" else 3600 * 2
                    hist = from_fyers_history(dc.history(inst.spot, FYERS_RESOLUTION[tf], now - span, now))
                hub.aggs[inst.spot].backfill(tf, hist)
            except Exception as e:
                log.info("backfill %s %s skipped: %s", key, tf, e)


def network_status() -> dict:
    out = {"ip_binding": settings.enable_ip_binding, "binding_error": S.binding_error, "feed": hub.status(), "apps": {}}
    for role in ("trading", "data"):
        app = settings.trading if role == "trading" else settings.data
        r = S.rest[role]
        tok = S.db.one("SELECT issued_at, expires_at FROM tokens WHERE role=?", (role,))
        out["apps"][role] = {
            "configured": app.configured, "app_id": app.app_id[:6] + "…" if app.app_id else "",
            "source_ip": role_ip(role), "ip_slot": "IP_A" if role == "trading" else "IP_B",
            "authed": r.authed, "token_expires": tok["expires_at"] if tok and r.authed else None,
            "rest": r.stats.snapshot(r.bucket, r.sf),
        }
    _, via = data_client()
    out["data_via"] = via or None
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    S.db = DB(settings.db_path)
    try:
        S.routes = binding.configure(settings.enable_ip_binding, settings.trading.source_ip, settings.data.source_ip)
    except binding.BindingError as e:
        # Never silently fall back to the wrong IP: orders from a non-whitelisted IP are rejected by Fyers anyway.
        S.binding_error = str(e)
        S.routes = {"trading": None, "data": None}
        log.error("IP binding misconfigured: %s. Fyers calls are disabled until fixed.", e)
    S.rest = {
        role: FyersRest(role, app.app_id, S.routes.get(role), settings.rest_rate_per_sec, settings.rest_burst,
                        settings.rest_max_retries, settings.quote_cache_ms)
        for role, app in (("trading", settings.trading), ("data", settings.data))
    }
    if not S.binding_error:
        for role in ("trading", "data"):
            tok = fauth.load(S.db, role)
            if tok:
                S.rest[role].set_token(tok["app_id"], tok["access_token"])
    S.engine = Engine(S.db, settings)
    bus.bind_loop(asyncio.get_running_loop())
    flusher = asyncio.create_task(bus.tick_flusher())
    await asyncio.to_thread(start_feeds)
    S.engine.start()
    log.info("Viper up on %s:%s (env=%s, feed=%s, live_trading=%s)", settings.host, settings.port, settings.env,
             hub.kind, settings.live_trading_enabled)
    yield
    flusher.cancel()
    S.engine.stop()
    if hub.feed:
        hub.feed.stop()
    if hub.order_feed:
        hub.order_feed.stop()


app = FastAPI(title="Viper", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


# ------------------------------------------------------------------- auth
def _sign(v: str) -> str:
    key = (settings.session_secret or settings.ui_password).encode()
    return hmac.new(key, v.encode(), hashlib.sha256).hexdigest()


def _authed(request_cookies: dict) -> bool:
    if not settings.ui_password:
        return True
    c = request_cookies.get("viper_session", "")
    exp, _, sig = c.partition(".")
    return bool(exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp)))


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    open_paths = ("/login", "/api/login", "/css/", "/js/", "/favicon", "/api/auth/callback")
    if not _authed(request.cookies) and not p.startswith(open_paths):
        if p.startswith("/api"):
            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


@app.get("/login")
def login_page():
    return FileResponse(WEB / "login.html")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not settings.ui_password or not hmac.compare_digest(str(body.get("password", "")), settings.ui_password):
        await asyncio.sleep(1)
        raise HTTPException(401, "wrong password")
    exp = str(int(time.time()) + 12 * 3600)
    r = JSONResponse({"ok": True})
    r.set_cookie("viper_session", f"{exp}.{_sign(exp)}", httponly=True, samesite="strict",
                 secure=settings.public_base_url.startswith("https"), max_age=12 * 3600)
    return r


# ------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not _authed(sock.cookies):
        await sock.close(code=4401)
        return
    await sock.accept()
    q = bus.add_client()
    client = secrets.token_hex(4)
    try:
        await sock.send_text(json.dumps({"t": "hello", "d": {"client": client, **bootstrap()}}, default=str))
        snap = list(hub.last.values())
        if snap:
            await sock.send_text(json.dumps({"t": "ticks", "d": snap}, default=str))

        async def pump():
            while True:
                await sock.send_text(await q.get())

        async def drain():
            while True:
                await sock.receive_text()  # keepalive pings from the browser

        await asyncio.gather(pump(), drain())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.remove_client(q)
        chains.unwatch_client(client)


def bootstrap() -> dict:
    return {
        "instruments": [{"key": k, "label": i.label, "spot": i.spot, "step": i.strike_step} for k, i in INSTRUMENTS.items()],
        "vix": VIX_SYMBOL, "timeframes": list(TIMEFRAMES), "risk": S.engine.risk.as_dict(),
        "network": network_status(), "live_trading_enabled": settings.live_trading_enabled, "env": settings.env,
        "server_time": time.time(),
    }


# ------------------------------------------------------------- status/api
@app.get("/api/status")
def status():
    return {"ok": True, "uptime_s": round(time.time() - S.started), **bootstrap(), "bus_clients": len(bus.clients)}


@app.get("/api/network")
def network():
    return network_status()


@app.get("/api/auth/{role}/begin")
def auth_begin(role: str):
    app_cfg = {"trading": settings.trading, "data": settings.data}.get(role)
    if not app_cfg:
        raise HTTPException(404, "role must be trading or data")
    if S.binding_error:
        raise HTTPException(409, f"fix IP binding first: {S.binding_error}")
    try:
        return {"url": fauth.begin(app_cfg)}
    except FyersError as e:
        raise HTTPException(400, str(e))


@app.get("/api/auth/callback")
def auth_callback(auth_code: str = "", state: str = "", s: str = "", code: str = "", message: str = ""):
    role = fauth.role_for_state(state)
    if not role:
        return RedirectResponse("/#market?auth=expired")
    if s and s != "ok" or not auth_code:
        return RedirectResponse(f"/#market?auth=failed&role={role}")
    app_cfg = settings.trading if role == "trading" else settings.data
    try:
        token = fauth.exchange(app_cfg, auth_code, role_ip(role))
        S.rest[role].set_token(app_cfg.app_id, token)
        profile = {}
        try:
            profile = S.rest[role].profile().get("data", {})
        except FyersError:
            pass
        fauth.store(S.db, role, app_cfg.app_id, token, {"name": profile.get("name"), "fy_id": profile.get("fy_id")})
    except FyersError as e:
        log.error("auth %s failed: %s", role, e)
        return RedirectResponse(f"/#market?auth=failed&role={role}")
    start_feeds()
    bus.publish("auth", {"role": role, "ok": True}, local=False)
    return RedirectResponse(f"/#market?auth=ok&role={role}")


@app.post("/api/auth/{role}/logout")
def auth_logout(role: str):
    if role not in S.rest:
        raise HTTPException(404)
    S.rest[role].set_token(S.rest[role].app_id, "")
    S.db.x("DELETE FROM tokens WHERE role=?", (role,))
    start_feeds()
    return network_status()


@app.post("/api/feed/restart")
def feed_restart():
    start_feeds()
    return network_status()


# ---------------------------------------------------------------- market
def _key(k: str) -> str:
    if k not in INSTRUMENTS:
        raise HTTPException(400, f"unknown instrument {k}")
    return k


@app.get("/api/candles")
def candles(key: str, tf: str = "1m", n: int = Query(400, le=3000)):
    _key(key)
    if tf not in TIMEFRAMES:
        raise HTTPException(400, "bad timeframe")
    sym = INSTRUMENTS[key].spot
    sec = TIMEFRAMES[tf]
    live = hub.aggs[sym].get(tf, n)
    hist: list[dict] = []
    source = "ticks"
    if S.synthetic is not None:
        if len(live) < n:
            hist = S.synthetic.history(key, sec, n - len(live), (live[0]["t"] if live else time.time()) - sec,
                                       live[0]["o"] if live else None)
        source = "synthetic"
    else:
        dc, _ = data_client()
        if dc is not None:
            span = sec * n * (3 if sec < 86400 else 1.6)  # pad for weekends/holidays
            if sec < 60:
                span = min(span, 86400 * 2)
            now = int(time.time())
            try:
                hist = from_fyers_history(dc.history(sym, FYERS_RESOLUTION[tf], now - int(span), now))
                source = "fyers+ticks"
            except FyersError as e:
                log.info("history %s %s unavailable (%s); using ticks only", key, tf, e)
    if hist:
        hub.aggs[sym].backfill(tf, hist)
    out = stitch(hist, live)[-n:]
    return {"key": key, "tf": tf, "source": source, "candles": sanitize(out), "tf_s": sec}


@app.get("/api/chain")
def chain(key: str, expiry: str = "", client: str = ""):
    _key(key)
    try:
        if client:
            return chains.watch_client(client, key, expiry)
        return chains.snapshot(key, expiry)
    except NotAuthenticated as e:
        raise HTTPException(401, str(e))
    except (FyersError, RuntimeError) as e:
        raise HTTPException(503, str(e))


@app.get("/api/quotes")
def quotes():
    return {"ticks": list(hub.last.values())}


# ----------------------------------------------------------- risk/positions
@app.get("/api/risk")
def risk_get():
    return {"config": S.engine.risk.as_dict(), "deployable": S.engine.deployable_equity(),
            "available_margin": S.engine.live.funds_cache.get("available"),
            "blockers": {k: S.engine.live_blockers(k) for k in INSTRUMENTS}}


@app.post("/api/risk")
async def risk_set(request: Request):
    patch = await request.json()
    allowed = {"equity_allocation", "max_capital_pct", "max_sl_pct", "max_hold_min", "daily_loss_pct",
               "max_open_live", "automation", "kill_switch"}
    bad = set(patch) - allowed
    if bad:
        raise HTTPException(400, f"unknown fields: {sorted(bad)}")
    try:
        cfg = await asyncio.to_thread(S.engine.save_risk, patch)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return cfg.as_dict()


@app.get("/api/positions")
def positions():
    broker = None
    if S.engine.live.ready:
        try:
            broker = S.rest["trading"].positions()
        except FyersError as e:
            broker = {"error": str(e)}
    return {"live": S.engine.live.snapshot(), "broker": broker}


@app.post("/api/positions/{tid}/close")
def position_close(tid: int):
    if tid not in S.engine.live.open:
        raise HTTPException(404, "no open live trade with that id")
    S.engine.live.request_exit(tid, "manual")
    return {"ok": True}


@app.post("/api/positions/{tid}/modify")
async def position_modify(tid: int, request: Request):
    b = await request.json()
    try:
        return S.engine.live.modify(tid, b.get("stop"), b.get("target"))
    except KeyError:
        raise HTTPException(404, "no open live trade with that id")


@app.post("/api/orders/deploy")
async def deploy(request: Request):
    b = await request.json()
    try:
        row = await asyncio.to_thread(
            S.engine.manual_deploy, _key(b["key"]), b["option_type"], int(b.get("offset", 0)), int(b["lots"]),
            float(b.get("stop_pct", 20)), float(b.get("target_pct", 30)), float(b.get("hold_min", S.engine.risk.max_hold_min)),
            b.get("symbol") or None)
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return S.engine.live.public(row)


@app.post("/api/kill")
def kill():
    cfg = S.engine.save_risk({"kill_switch": True})
    return {"kill_switch": cfg.kill_switch, "open_live": len(S.engine.live.open)}


# ---------------------------------------------------------------- journal
@app.get("/api/journal")
def journal_list(mode: str = "both", date_mode: str = "all", day: str | None = None, start: str | None = None,
                 end: str | None = None, instrument: str = "ALL", limit: int = Query(500, le=5000)):
    try:
        rows = journal.query(S.db, limit, mode=mode, date_mode=date_mode, day=day, start=start, end=end, instrument=instrument)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for r in rows:
        r.pop("meta", None)
    return {"rows": rows, "stats": {m: journal.stats([r for r in rows if r["mode"] == m]) for m in ("live", "shadow")}}


@app.get("/api/journal/export.csv")
def journal_export(mode: str = "both", date_mode: str = "today", day: str | None = None, start: str | None = None,
                   end: str | None = None, instrument: str = "ALL"):
    try:
        rows = journal.query(S.db, 100000, mode=mode, date_mode=date_mode, day=day, start=start, end=end, instrument=instrument)
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = journal.filename(mode, date_mode, day, start, end, instrument)
    return Response(journal.to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------ learnings
@app.post("/api/learn/instruction")
async def learn_instruction(scope: str = Form("ALL"), method: str = Form("text"), text: str = Form(""),
                            file: UploadFile | None = File(None)):
    if scope != "ALL":
        _key(scope)
    if method not in ("text", "pdf", "image"):
        raise HTTPException(400, "method must be text, pdf or image")
    blob, fname, mtype = None, None, ""
    if file is not None:
        blob = await file.read()
        if len(blob) > 20 * 1024 * 1024:
            raise HTTPException(413, "file too large (20 MB max)")
        fname, mtype = file.filename, file.content_type or ""
        if method == "image" and mtype not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise HTTPException(400, "image must be PNG, JPEG, WebP or GIF")
    if method == "text" and not text.strip():
        raise HTTPException(400, "empty instruction")
    if method != "text" and not blob:
        raise HTTPException(400, "file required")
    res = await asyncio.to_thread(ingest, S.db, scope, method, text, fname, blob, mtype)
    bus.publish("rules", {"added": len(res["rules"])}, local=False)
    return res


@app.get("/api/learn/rules")
def learn_rules():
    rows = S.db.q("SELECT * FROM rules ORDER BY created DESC")
    for r in rows:
        r["params"] = json.loads(r["params"])
    return {"rules": rows}


@app.patch("/api/learn/rules/{rid}")
async def learn_rule_toggle(rid: int, request: Request):
    b = await request.json()
    S.db.update("rules", "id", rid, {"enabled": 1 if b.get("enabled") else 0})
    return {"ok": True}


@app.delete("/api/learn/rules/{rid}")
def learn_rule_delete(rid: int):
    S.db.x("DELETE FROM rules WHERE id=?", (rid,))
    return {"ok": True}


@app.get("/api/learn/instructions")
def learn_instructions():
    rows = S.db.q("SELECT id, created, scope, method, filename, status, substr(text,1,400) AS text, extracted FROM instructions ORDER BY created DESC LIMIT 100")
    for r in rows:
        r["extracted"] = json.loads(r["extracted"] or "{}")
    return {"instructions": rows}


@app.get("/api/learn/lessons")
def learn_lessons(mode: str = "both", limit: int = Query(300, le=2000)):
    if mode in ("live", "shadow"):
        rows = S.db.q("SELECT * FROM lessons WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))
    else:
        rows = S.db.q("SELECT * FROM lessons ORDER BY ts DESC LIMIT ?", (limit,))
    return {"lessons": rows}


@app.get("/api/learn/state")
def learn_state():
    return {"matrix": S.engine.learner.matrix(), "params": S.engine.learner.params(),
            "cycle": S.engine.learner.cycle_progress()}


# --------------------------------------------------------------- shadow
@app.get("/api/shadow")
def shadow_state():
    eq = S.db.q("SELECT ts, equity FROM equity WHERE mode='shadow' ORDER BY ts DESC LIMIT 2000")
    eq.reverse()
    rows = S.db.q("SELECT * FROM trades WHERE mode='shadow' AND status='closed' AND exit_ts>=? ORDER BY exit_ts DESC",
                  (time.time() - settings.learning_window_days * 86400,))
    return {
        "equity": S.engine.shadow.equity(), "start_capital": settings.shadow_start_capital, "curve": eq,
        "open": S.engine.shadow.snapshot(), "stats": journal.stats(rows), "decisions": list(S.engine.decisions)[:120],
        "cycle": S.engine.learner.cycle_progress(), "matrix": S.engine.learner.matrix(),
        "features": S.engine.features, "regimes": S.engine.regimes,
    }


@app.get("/api/overview")
def overview():
    from viper.engine.clock import day_start

    t0 = day_start()
    live_today = S.db.q("SELECT * FROM trades WHERE mode='live' AND entry_ts>=? AND status IN ('open','closed')", (t0,))
    sh_today = S.db.q("SELECT * FROM trades WHERE mode='shadow' AND entry_ts>=? AND status IN ('open','closed')", (t0,))
    window = S.db.q("SELECT * FROM trades WHERE mode='shadow' AND status='closed' AND exit_ts>=?",
                    (time.time() - settings.learning_window_days * 86400,))
    last_lesson = S.db.one("SELECT * FROM lessons WHERE patch_json IS NOT NULL ORDER BY ts DESC LIMIT 1")
    cfg = S.engine.risk
    deploy = S.engine.deployable_equity()
    deployed = sum(t["entry_price"] * t["qty"] for t in S.engine.live.snapshot())
    hours = max(1 / 60, (time.time() - max(t0 + 9.25 * 3600, min([r["entry_ts"] for r in live_today] or [time.time()]))) / 3600)
    return {
        "shadow": {
            "equity": S.engine.shadow.equity(), "today": journal.stats(sh_today), "window": journal.stats(window),
            "open": len(S.engine.shadow.open), "realized": S.engine.shadow.realized_today(),
            "unrealized": S.engine.shadow.unrealized(), "cycle": S.engine.learner.cycle_progress(),
            "last_patch": last_lesson,
        },
        "live": {
            "deployable": deploy, "deployed": round(deployed, 2), "open": len(S.engine.live.open),
            "realized": S.engine.live.realized_today(), "unrealized": S.engine.live.unrealized(),
            "today": journal.stats(live_today), "per_hour": round(len(live_today) / hours, 2),
            "automation": cfg.automation, "kill_switch": cfg.kill_switch,
        },
        "overall": {
            "regimes": S.engine.regimes, "feed": hub.status(), "network": network_status(),
            "risk_util": round(100 * deployed / (deploy * cfg.max_capital_pct / 100 * max(1, cfg.max_open_live)), 1) if deploy else 0,
            "daily_loss_used": round(100 * -min(0, S.engine.live.realized_today() + S.engine.live.unrealized()) / (deploy * cfg.daily_loss_pct / 100), 1) if deploy else 0,
            "server_time": datetime.now(IST).isoformat(),
        },
    }


# ------------------------------------------------------------- drawings
@app.get("/api/drawings")
def drawings_get(key: str):
    rows = S.db.q("SELECT * FROM drawings WHERE instrument=? ORDER BY updated", (_key(key),))
    return {"drawings": [{"id": r["id"], "kind": r["kind"], **json.loads(r["data"])} for r in rows]}


@app.put("/api/drawings")
async def drawings_put(request: Request):
    b = await request.json()
    key = _key(b["key"])
    S.db.x("DELETE FROM drawings WHERE instrument=?", (key,))
    for d in b.get("drawings", [])[:500]:
        did, kind = str(d.get("id"))[:40], str(d.get("kind"))[:20]
        data = {k: v for k, v in d.items() if k not in ("id", "kind")}
        S.db.x("INSERT OR REPLACE INTO drawings(id,instrument,kind,data,updated) VALUES(?,?,?,?,?)",
               (did, key, kind, json.dumps(data), time.time()))
    return {"ok": True}


# ---------------------------------------------------------------- static
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(WEB / "favicon.svg", media_type="image/svg+xml")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


app.mount("/css", StaticFiles(directory=WEB / "css"), name="css")
app.mount("/js", StaticFiles(directory=WEB / "js"), name="js")
