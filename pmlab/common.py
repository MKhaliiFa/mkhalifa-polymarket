"""Shared pieces of the strategy lab: settings, fees, book parsing, storage. Read-only, public data only."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from pmbot.feeds import http_json, FeedError  # noqa: F401  (re-exported for the lab modules)


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class LabConfig:
    gamma: str = os.getenv("PM_GAMMA_URL", "https://gamma-api.polymarket.com")
    clob: str = os.getenv("PM_CLOB_URL", "https://clob.polymarket.com")
    data: str = os.getenv("PM_DATA_URL", "https://data-api.polymarket.com/v2")
    data_dir: str = os.getenv("PM_DATA_DIR", "/opt/mkhalifa-polymarket/data")
    # S1
    arb_every_s: float = _f("LAB_ARB_EVERY_S", 60)
    arb_events: int = int(_f("LAB_ARB_EVENTS", 500))
    arb_budget: float = _f("LAB_ARB_BUDGET", 100)
    arb_recheck_s: float = _f("LAB_ARB_RECHECK_S", 5)
    # S2
    mm_every_s: float = _f("LAB_MM_EVERY_S", 60)
    mm_markets: int = int(_f("LAB_MM_MARKETS", 10))
    mm_inv_mult: float = _f("LAB_MM_INV_MULT", 3)
    # S4
    copy_every_s: float = _f("LAB_COPY_EVERY_S", 60)
    copy_leaders: int = int(_f("LAB_COPY_LEADERS", 20))
    copy_min_usd: float = _f("LAB_COPY_MIN_USD", 100)
    copy_stake: float = _f("LAB_COPY_STAKE", 10)
    copy_refresh_s: float = _f("LAB_COPY_REFRESH_S", 6 * 3600)


def maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def fee_rate(market: dict) -> float:
    """Taker fee rate of a Gamma market (0 when fees are off)."""
    if not market.get("feesEnabled"):
        return 0.0
    fs = maybe_json(market.get("feeSchedule")) or {}
    try:
        return float(fs.get("rate") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def rebate_rate(market: dict) -> float:
    fs = maybe_json(market.get("feeSchedule")) or {}
    try:
        return float(fs.get("rebateRate") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def taker_fee(rate: float, p: float) -> float:
    """Fee per share for a taker at price p."""
    return rate * p * (1 - p)


def levels(book: dict, side: str) -> list[tuple[float, float]]:
    """Order book side as [(price, size)], best first."""
    out = []
    for x in book.get(side) or []:
        try:
            out.append((float(x["price"]), float(x["size"])))
        except (KeyError, TypeError, ValueError):
            pass
    out.sort(key=lambda t: t[0], reverse=(side == "bids"))
    return [t for t in out if t[1] > 0]


def fetch_books(cfg: LabConfig, token_ids: list[str], get=http_json) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(token_ids), 50):
        chunk = token_ids[i:i + 50]
        rows = get(f"{cfg.clob}/books", [{"token_id": t} for t in chunk]) or []
        for b in rows:
            if isinstance(b, dict):
                out[str(b.get("asset_id"))] = {"bids": levels(b, "bids"), "asks": levels(b, "asks")}
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS arb (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_id TEXT, title TEXT, kind TEXT, n INTEGER,
  sets REAL, cost REAL, profit REAL, recheck_profit REAL, captured INTEGER, profit_counted REAL);
CREATE TABLE IF NOT EXISTS mm_fills (
  ts REAL, market TEXT, side TEXT, price REAL, size REAL, rebate REAL, day TEXT);
CREATE TABLE IF NOT EXISTS mm_marks (
  ts REAL, day TEXT, cash REAL, inv_value REAL, rewards_est REAL, capital REAL);
CREATE TABLE IF NOT EXISTS mm_book (
  market TEXT PRIMARY KEY, token TEXT, title TEXT, inv REAL, cash REAL, rebates REAL, rewards_est REAL,
  last_trade_ts REAL, active INTEGER, mid REAL, settled INTEGER DEFAULT 0, info TEXT);
CREATE TABLE IF NOT EXISTS copies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, leader TEXT, condition TEXT, token TEXT, title TEXT,
  leader_price REAL, entry REAL, shares REAL, fee REAL, status TEXT DEFAULT 'open',
  exit REAL, exit_fee REAL, pnl REAL, closed_ts REAL, how TEXT);
CREATE TABLE IF NOT EXISTS leaders (wallet TEXT PRIMARY KEY, name TEXT, pnl REAL, volume REAL, last_ts REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS errors (ts REAL, where_ TEXT, msg TEXT);
"""


class LabStore:
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

    def error(self, ts: float, where: str, msg: str) -> None:
        self.run("INSERT INTO errors VALUES(?,?,?)", ts, where, str(msg)[:300])
