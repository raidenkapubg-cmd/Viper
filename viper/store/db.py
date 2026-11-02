"""SQLite persistence (WAL mode, one connection per thread).

Tables
  kv              - small settings blobs (risk config, automation matrix, equity cap)
  tokens          - daily Fyers access tokens per role
  trades          - live AND shadow trades (``mode`` column), with MFE/MAE audit fields
  lessons         - post-mortem ledger (mistake, where, rule patch)
  rules           - structured strategy rules from text/PDF/image instructions
  instructions    - raw instruction submissions
  strategy_stats  - learner state per (strategy, regime, instrument)
  equity          - equity curve samples (shadow + live)
  drawings        - persistent chart drawings per instrument
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
  role TEXT PRIMARY KEY, app_id TEXT NOT NULL, access_token TEXT NOT NULL,
  issued_at REAL NOT NULL, expires_at REAL NOT NULL, profile TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL CHECK(mode IN ('live','shadow')),
  instrument TEXT NOT NULL, symbol TEXT NOT NULL, side INTEGER NOT NULL,
  strike REAL, option_type TEXT, spot_at_entry REAL,
  qty INTEGER NOT NULL, lots INTEGER NOT NULL DEFAULT 1,
  entry_ts REAL NOT NULL, entry_price REAL NOT NULL,
  exit_ts REAL, exit_price REAL,
  stop_price REAL, target_price REAL, max_hold_s INTEGER,
  gross_pnl REAL, fees REAL, net_pnl REAL,
  strategy TEXT NOT NULL, regime TEXT, confidence REAL,
  exit_reason TEXT, status TEXT NOT NULL DEFAULT 'open',
  mfe REAL DEFAULT 0, mae REAL DEFAULT 0, mfe_ts REAL, mae_ts REAL,
  slippage REAL DEFAULT 0, broker_order_id TEXT, origin TEXT DEFAULT 'viper',
  meta TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_mode_ts ON trades(mode, entry_ts);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
  trade_id INTEGER, instrument TEXT, regime TEXT, interval TEXT,
  kind TEXT NOT NULL, mistake TEXT NOT NULL, patch TEXT, patch_json TEXT
);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, scope TEXT NOT NULL,
  kind TEXT NOT NULL, params TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
  enforced INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1, instruction_id INTEGER
);
CREATE TABLE IF NOT EXISTS instructions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, scope TEXT NOT NULL,
  method TEXT NOT NULL, filename TEXT, text TEXT, extracted TEXT, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_stats (
  strategy TEXT NOT NULL, regime TEXT NOT NULL, instrument TEXT NOT NULL,
  alpha REAL NOT NULL DEFAULT 1, beta REAL NOT NULL DEFAULT 1,
  weight REAL NOT NULL DEFAULT 1, n INTEGER NOT NULL DEFAULT 0, pnl REAL NOT NULL DEFAULT 0,
  updated REAL NOT NULL, PRIMARY KEY (strategy, regime, instrument)
);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL NOT NULL, mode TEXT NOT NULL, equity REAL NOT NULL, realized REAL, unrealized REAL
);
CREATE INDEX IF NOT EXISTS ix_equity ON equity(mode, ts);
CREATE TABLE IF NOT EXISTS drawings (
  id TEXT PRIMARY KEY, instrument TEXT NOT NULL, kind TEXT NOT NULL,
  data TEXT NOT NULL, updated REAL NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._wlock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            self._local.c = c
        return c

    # -- generic helpers -------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        r = self.conn().execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._wlock:
            cur = self.conn().execute(sql, tuple(args))
            return cur.lastrowid or cur.rowcount

    def insert(self, table: str, row: dict) -> int:
        cols = ",".join(row)
        ph = ",".join("?" for _ in row)
        return self.x(f"INSERT INTO {table} ({cols}) VALUES ({ph})", row.values())

    def update(self, table: str, key: str, key_val: Any, row: dict) -> None:
        if not row:
            return
        sets = ",".join(f"{k}=?" for k in row)
        self.x(f"UPDATE {table} SET {sets} WHERE {key}=?", [*row.values(), key_val])

    # -- kv ----------------------------------------------------------------
    def kv_get(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k: str, v: Any) -> None:
        self.x(
            "INSERT INTO kv(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
            (k, json.dumps(v), time.time()),
        )
