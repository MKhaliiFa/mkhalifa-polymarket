"""SQLite storage: every snapshot, window and paper trade, so the numbers can be checked later."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  ts REAL, asset TEXT, slug TEXT, price REAL, strike REAL, tau REAL, sigma REAL, fair_up REAL,
  up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL);
CREATE INDEX IF NOT EXISTS snap_ts ON snapshots(ts);
CREATE TABLE IF NOT EXISTS windows (
  slug TEXT PRIMARY KEY, asset TEXT, start REAL, strike REAL, close REAL,
  proxy_up INTEGER, official_up INTEGER, cal_fair_up REAL, cal_mid_up REAL, settled_ts REAL);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT, slug TEXT, side TEXT, ts REAL, tau REAL,
  price REAL, shares REAL, stake REAL, fee REAL, fair REAL, edge REAL,
  status TEXT DEFAULT 'open', won INTEGER, pnl REAL, settled_ts REAL, official INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS errors (ts REAL, msg TEXT);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def q(self, sql: str, *a):
        return self.db.execute(sql, a).fetchall()

    def one(self, sql: str, *a):
        return self.db.execute(sql, a).fetchone()

    def run(self, sql: str, *a):
        return self.db.execute(sql, a)

    def meta(self, k: str, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", k)
        return r["v"] if r else default

    def set_meta(self, k: str, v) -> None:
        self.run("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, str(v))

    def error(self, ts: float, msg: str) -> None:
        self.run("INSERT INTO errors VALUES(?,?)", ts, msg[:300])

    def realized(self, since: float = 0.0) -> float:
        r = self.one("SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE status='closed' AND settled_ts>=?", since)
        return float(r["s"])
