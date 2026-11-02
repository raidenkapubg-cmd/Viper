"""Singleton market-data multiplexer.

One ``FyersDataSocket`` (data app, bound to the data IP) and one
``FyersOrderSocket`` (trading app, bound to the trading IP) exist per process.
Every consumer (charts, option chain, engine, every browser tab) subscribes
through this hub with reference counting, so a symbol is subscribed to Fyers once
no matter how many tabs show it, and dropped when the last consumer leaves.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from viper.bus import bus
from viper.market.candles import SeriesAggregator
from viper.market.instruments import INSTRUMENTS, VIX_SYMBOL
from viper.market.synthetic import SyntheticFeed
from viper.net.binding import bound_to

log = logging.getLogger("viper.hub")

CORE_SYMBOLS = [i.spot for i in INSTRUMENTS.values()] + [VIX_SYMBOL]


def normalize_fyers_tick(m: dict) -> dict | None:
    sym = m.get("symbol")
    ltp = m.get("ltp")
    if not sym or ltp is None:
        return None
    ts = m.get("exch_feed_time") or m.get("last_traded_time") or time.time()
    return {
        "symbol": sym, "ltp": ltp,
        "bid": m.get("bid_price"), "ask": m.get("ask_price"),
        "open": m.get("open_price"), "high": m.get("high_price"), "low": m.get("low_price"),
        "prev_close": m.get("prev_close_price"),
        "vol": m.get("vol_traded_today"), "oi": m.get("OI"),
        "tbq": m.get("tot_buy_qty"), "tsq": m.get("tot_sell_qty"),
        "ts": float(ts),
    }


class LiveDataFeed:
    """Wraps fyers_apiv3 FyersDataSocket. Runs on the data app's IP."""

    name = "live"

    def __init__(self, on_tick: Callable[[dict], None], app_id: str, token: str, source_ip: str | None, log_dir: Path):
        self.on_tick = on_tick
        self.app_id, self.token, self.source_ip = app_id, token, source_ip
        self.log_dir = log_dir
        self.symbols: set[str] = set()
        self.sock = None
        self.connected = False
        self.last_msg_ts = 0.0
        self.last_error = ""

    def start(self) -> None:
        from fyers_apiv3.FyersWebsocket import data_ws

        data_ws.FyersDataSocket._instance = None  # SDK class is a singleton; force a clean instance on reconnect
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.sock = data_ws.FyersDataSocket(
            access_token=f"{self.app_id}:{self.token}",
            log_path=str(self.log_dir),
            litemode=False,
            # With a callback set, write_to_file only makes the SDK's socket thread a daemon
            # (otherwise a non-daemon thread blocks clean shutdown under systemd).
            write_to_file=True,
            reconnect=True,
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
            reconnect_retry=50,
        )
        with bound_to(self.source_ip):  # symbol-token REST lookups during connect
            self.sock.connect()

    def _on_connect(self) -> None:
        self.connected = True
        log.info("data socket connected")
        if self.symbols:
            self._send_sub(sorted(self.symbols))

    def _on_close(self, msg) -> None:
        self.connected = False
        log.warning("data socket closed: %s", msg)

    def _on_error(self, msg) -> None:
        self.last_error = str(msg)
        log.warning("data socket error: %s", msg)

    def _on_message(self, msg) -> None:
        if not isinstance(msg, dict) or msg.get("type") not in ("sf", "if"):
            return
        self.last_msg_ts = time.time()
        t = normalize_fyers_tick(msg)
        if t:
            self.on_tick(t)

    def _send_sub(self, symbols: list[str]) -> None:
        if self.sock is None:
            return
        with bound_to(self.source_ip):
            self.sock.subscribe(symbols=symbols, data_type="SymbolUpdate")

    def subscribe(self, symbols: list[str]) -> None:
        new = [s for s in symbols if s not in self.symbols]
        self.symbols.update(new)
        if new and self.connected:
            self._send_sub(new)

    def unsubscribe(self, symbols: list[str]) -> None:
        gone = [s for s in symbols if s in self.symbols]
        self.symbols.difference_update(gone)
        if gone and self.connected and self.sock is not None:
            with bound_to(self.source_ip):
                self.sock.unsubscribe(symbols=gone, data_type="SymbolUpdate")

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close_connection()
            except Exception:
                log.exception("closing data socket")
        self.connected = False


class LiveOrderFeed:
    """Wraps fyers_apiv3 FyersOrderSocket. Runs on the trading app's IP."""

    def __init__(self, app_id: str, token: str, log_dir: Path, handlers: dict[str, Callable[[dict], None]]):
        self.app_id, self.token, self.log_dir, self.handlers = app_id, token, log_dir, handlers
        self.sock = None
        self.connected = False

    def start(self) -> None:
        from fyers_apiv3.FyersWebsocket import order_ws

        order_ws.FyersOrderSocket._instance = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.sock = order_ws.FyersOrderSocket(
            access_token=f"{self.app_id}:{self.token}",
            write_to_file=True,  # daemon socket thread; order events are also logged under data/logs
            log_path=str(self.log_dir),
            on_connect=self._on_connect,
            on_close=lambda m: self._set(False, m),
            on_error=lambda m: log.warning("order socket error: %s", m),
            on_orders=lambda m: self._dispatch("orders", m),
            on_trades=lambda m: self._dispatch("trades", m),
            on_positions=lambda m: self._dispatch("positions", m),
            on_general=lambda m: self._dispatch("general", m),
            reconnect=True,
            reconnect_retry=50,
        )
        self.sock.connect()  # run_forever hook binds this socket to the trading IP

    def _set(self, ok: bool, msg=None) -> None:
        self.connected = ok
        if not ok:
            log.warning("order socket closed: %s", msg)

    def _on_connect(self) -> None:
        self.connected = True
        self.sock.subscribe(data_type="OnOrders,OnTrades,OnPositions")

    def _dispatch(self, kind: str, msg: dict) -> None:
        fn = self.handlers.get(kind)
        if fn and isinstance(msg, dict):
            fn(msg.get(kind, msg))

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close_connection()
            except Exception:
                log.exception("closing order socket")
        self.connected = False


class MarketHub:
    def __init__(self) -> None:
        self.feed: SyntheticFeed | LiveDataFeed | None = None
        self.order_feed: LiveOrderFeed | None = None
        self.last: dict[str, dict] = {}
        self.aggs: dict[str, SeriesAggregator] = {s: SeriesAggregator(s) for s in CORE_SYMBOLS}
        self.refs: dict[str, set[str]] = {}
        self.lock = threading.RLock()
        self.ticks = 0

    @property
    def kind(self) -> str:
        return self.feed.name if self.feed else "offline"

    @property
    def synthetic(self) -> bool:
        return isinstance(self.feed, SyntheticFeed)

    def attach(self, feed) -> None:
        with self.lock:
            if self.feed is not None:
                self.feed.stop()
            self.feed = feed
            self.last.clear()
            for a in self.aggs.values():
                for dq in a.bars.values():
                    dq.clear()
            feed.subscribe(sorted(set(CORE_SYMBOLS) | set(self.refs)))
        feed.start()
        bus.publish("feed", self.status())

    def subscribe(self, owner: str, symbols: list[str]) -> None:
        new = []
        with self.lock:
            for s in symbols:
                holders = self.refs.setdefault(s, set())
                if not holders and s not in CORE_SYMBOLS:
                    new.append(s)
                holders.add(owner)
        if new and self.feed:
            self.feed.subscribe(new)

    def release(self, owner: str, symbols: list[str] | None = None) -> None:
        gone = []
        with self.lock:
            for s in list(self.refs) if symbols is None else symbols:
                holders = self.refs.get(s)
                if not holders:
                    continue
                holders.discard(owner)
                if not holders:
                    del self.refs[s]
                    if s not in CORE_SYMBOLS:
                        gone.append(s)
        if gone and self.feed:
            self.feed.unsubscribe(gone)

    def on_tick(self, t: dict) -> None:
        sym = t["symbol"]
        with self.lock:
            prev = self.last.get(sym, {})
            merged = {**prev, **{k: v for k, v in t.items() if v is not None}}
            self.last[sym] = merged
            agg = self.aggs.get(sym)
        self.ticks += 1
        bus.publish_tick(merged)
        bus._emit_local("tick", merged)
        if agg:
            for tf, candle, closed in agg.on_tick(merged["ts"], merged["ltp"], merged.get("vol")):
                if closed and tf in ("5s", "1m", "5m"):
                    bus._emit_local("bar_close", {"symbol": sym, "tf": tf})

    def ltp(self, symbol: str) -> float | None:
        t = self.last.get(symbol)
        return t.get("ltp") if t else None

    def quote(self, symbol: str) -> dict | None:
        return self.last.get(symbol)

    def status(self) -> dict:
        f = self.feed
        return {
            "kind": self.kind,
            "synthetic": self.synthetic,
            "connected": bool(f and f.connected),
            "symbols": len(set(CORE_SYMBOLS) | set(self.refs)),
            "ticks": self.ticks,
            "last_msg_ts": getattr(f, "last_msg_ts", None),
            "order_socket": bool(self.order_feed and self.order_feed.connected),
        }


hub = MarketHub()
