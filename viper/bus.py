"""Thread-safe pub/sub bridging Fyers socket threads to browser WebSockets.

Feed threads call ``publish`` / ``publish_tick`` from anywhere. Ticks are coalesced
per symbol and flushed as a single ``ticks`` batch every ``TICK_FLUSH_S``, so a
burst of 500 ticks/s becomes ~4 messages/s per browser tab regardless of how many
tabs are open. Each tab holds one local WebSocket; nothing in the UI polls Fyers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger("viper.bus")
TICK_FLUSH_S = 0.25


class Bus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self._pending_ticks: dict[str, dict] = {}
        self._tick_lock = threading.Lock()
        self._listeners: dict[str, list] = {}
        self.sent = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # internal (in-process) listeners, called synchronously on the publisher thread
    def on(self, topic: str, fn) -> None:
        self._listeners.setdefault(topic, []).append(fn)

    def _emit_local(self, topic: str, data: Any) -> None:
        for fn in self._listeners.get(topic, ()):
            try:
                fn(data)
            except Exception:
                log.exception("listener for %s failed", topic)

    def publish(self, topic: str, data: Any, local: bool = True) -> None:
        if local:
            self._emit_local(topic, data)
        self._broadcast({"t": topic, "d": data, "ts": time.time()})

    def publish_tick(self, tick: dict) -> None:
        with self._tick_lock:
            self._pending_ticks[tick["symbol"]] = tick

    def _broadcast(self, msg: dict) -> None:
        if not self.loop or not self.clients:
            return
        raw = json.dumps(msg, default=_json_default, separators=(",", ":"))
        self.loop.call_soon_threadsafe(self._fanout, raw)

    def _fanout(self, raw: str) -> None:
        for q in list(self.clients):
            if q.qsize() > 400:  # slow tab: drop oldest rather than stall everyone
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(raw)
            self.sent += 1

    async def tick_flusher(self) -> None:
        while True:
            await asyncio.sleep(TICK_FLUSH_S)
            with self._tick_lock:
                batch, self._pending_ticks = self._pending_ticks, {}
            if batch:
                self._broadcast({"t": "ticks", "d": list(batch.values()), "ts": time.time()})

    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)


def _json_default(o: Any) -> Any:
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


bus = Bus()
