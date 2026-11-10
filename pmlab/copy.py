"""S4: copy the month's best wallets, with the delay a real follower has. Data API v2 (public) for the
leaders and their trades, CLOB books for our own paper price, Gamma for fees and resolutions."""
from __future__ import annotations

import math
import time

from .common import fee_rate, fetch_books, http_json, maybe_json, taker_fee


def pick_leaders(rows: list[dict], n: int) -> list[dict]:
    """Directional winners only: market makers (P&L under 5 % of volume) and tiny wallets are left out."""
    out = []
    for r in rows:
        try:
            pnl, vol = float(r.get("pnl") or 0), float(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        w = r.get("user_id") or r.get("proxy_wallet")
        if not w or pnl <= 0 or vol < 5000 or pnl / vol < 0.05:
            continue
        out.append({"wallet": str(w), "name": str(r.get("user_name") or "")[:40], "pnl": pnl, "volume": vol})
    return out[:n]


class Copy:
    def __init__(self, cfg, store, get=http_json, clock=time.time):
        self.cfg, self.store, self.get, self.clock = cfg, store, get, clock
        self.markets: dict[str, dict] = {}

    # ---- leaders
    def refresh_leaders(self) -> int:
        rows = (self.get(f"{self.cfg.data}/leaderboard?time_period=month&sort_by=pnl&limit=200") or {}).get("data") or []
        now = self.clock()
        keep = pick_leaders(rows, self.cfg.copy_leaders)
        old = {r["wallet"]: r["last_ts"] for r in self.store.q("SELECT wallet,last_ts FROM leaders")}
        self.store.run("DELETE FROM leaders")
        for L in keep:            # a new leader starts from now: its past trades are never copied
            self.store.run("INSERT INTO leaders VALUES(?,?,?,?,?)", L["wallet"], L["name"], L["pnl"], L["volume"],
                           old.get(L["wallet"], now))
        self.store.set_meta("leaders_ts", now)
        return len(keep)

    def market(self, condition: str) -> dict:
        if condition not in self.markets:
            rows = self.get(f"{self.cfg.gamma}/markets?condition_ids={condition}") or []
            self.markets[condition] = rows[0] if rows else {}
        return self.markets[condition]

    # ---- trades
    def poll(self) -> int:
        n = 0
        if self.clock() - float(self.store.meta("leaders_ts", 0)) > self.cfg.copy_refresh_s:
            self.refresh_leaders()
        for L in self.store.q("SELECT * FROM leaders"):
            rows = (self.get(f"{self.cfg.data}/trades?user={L['wallet']}&limit=100&start={int(L['last_ts']) + 1}") or {}).get("data") or []
            rows = sorted((r for r in rows if isinstance(r, dict)), key=lambda r: r.get("timestamp") or 0)
            last = float(L["last_ts"])
            for r in rows:
                ts = float(r.get("timestamp") or 0)
                if ts <= last:
                    continue
                last = ts
                n += self.on_trade(L["wallet"], r)
            self.store.run("UPDATE leaders SET last_ts=? WHERE wallet=?", last, L["wallet"])
        return n

    def on_trade(self, wallet: str, r: dict) -> int:
        tok, cond = str(r.get("token_id") or ""), str(r.get("condition_id") or "")
        try:
            size, price = float(r.get("size") or 0), float(r.get("price") or 0)
        except (TypeError, ValueError):
            return 0
        if not tok or not cond:
            return 0
        if r.get("side") == "SELL":
            return self.close_on_sell(wallet, tok, cond)
        if r.get("side") != "BUY" or size * price < self.cfg.copy_min_usd:
            return 0
        if self.store.one("SELECT 1 FROM copies WHERE leader=? AND token=? AND status='open'", wallet, tok):
            return 0
        book = fetch_books(self.cfg, [tok], self.get).get(tok) or {}
        if not book.get("asks"):
            return 0
        ask = book["asks"][0][0]
        if ask >= 0.99:
            return 0
        rate = fee_rate(self.market(cond))
        fee = taker_fee(rate, ask)
        shares = self.cfg.copy_stake / (ask + fee)
        self.store.run("INSERT INTO copies(ts,leader,condition,token,title,leader_price,entry,shares,fee) VALUES(?,?,?,?,?,?,?,?,?)",
                       self.clock(), wallet, cond, tok, str(r.get("title") or "")[:120], price, ask, shares, fee * shares)
        return 1

    def close_on_sell(self, wallet: str, tok: str, cond: str) -> int:
        rows = self.store.q("SELECT * FROM copies WHERE leader=? AND token=? AND status='open'", wallet, tok)
        if not rows:
            return 0
        book = fetch_books(self.cfg, [tok], self.get).get(tok) or {}
        bid = book["bids"][0][0] if book.get("bids") else 0.0
        rate = fee_rate(self.market(cond))
        for c in rows:
            xfee = taker_fee(rate, bid) * c["shares"]
            pnl = c["shares"] * (bid - c["entry"]) - c["fee"] - xfee
            self.store.run("UPDATE copies SET status='closed', exit=?, exit_fee=?, pnl=?, closed_ts=?, how='leader sold' WHERE id=?",
                           bid, xfee, pnl, self.clock(), c["id"])
        return len(rows)

    def resolve(self) -> int:
        n = 0
        conds = [r["condition"] for r in self.store.q("SELECT DISTINCT condition FROM copies WHERE status='open'")]
        for cond in conds:
            self.markets.pop(cond, None)             # fresh read: resolution status changes
            m = self.market(cond)
            if not m.get("closed"):
                continue
            toks = [str(t) for t in (maybe_json(m.get("clobTokenIds")) or [])]
            try:
                prices = [float(x) for x in (maybe_json(m.get("outcomePrices")) or [])]
            except (TypeError, ValueError):
                continue
            if len(prices) != len(toks) or max(prices, default=0) < 0.99:
                continue                              # closed but not settled yet
            for c in self.store.q("SELECT * FROM copies WHERE condition=? AND status='open'", cond):
                pay = prices[toks.index(c["token"])] if c["token"] in toks else 0.0
                pnl = c["shares"] * (pay - c["entry"]) - c["fee"]
                self.store.run("UPDATE copies SET status='closed', exit=?, exit_fee=0, pnl=?, closed_ts=?, how='resolved' WHERE id=?",
                               pay, pnl, self.clock(), c["id"])
                n += 1
        return n


def summary(store, since: float) -> dict:
    rows = store.q("SELECT pnl, condition, entry, leader_price FROM copies WHERE status='closed' AND closed_ts>=?", since)
    pn = [float(r["pnl"]) for r in rows]
    opened = store.one("SELECT COUNT(*) n FROM copies WHERE ts>=?", since)["n"]
    open_now = store.one("SELECT COUNT(*) n FROM copies WHERE status='open'")["n"]
    mean = sum(pn) / len(pn) if pn else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in pn) / (len(pn) - 1)) if len(pn) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(pn))) if sd > 0 else 0.0
    by: dict[str, float] = {}
    for r in rows:
        by[r["condition"]] = by.get(r["condition"], 0.0) + float(r["pnl"])
    gains = sum(v for v in by.values() if v > 0)
    top_share = max(by.values(), default=0.0) / gains if gains > 0 else 0.0
    slip = [float(r["entry"]) - float(r["leader_price"]) for r in rows if r["leader_price"] is not None]
    return {"opened": opened, "closed": len(pn), "open": open_now, "pnl": sum(pn), "t": t, "top_share": top_share,
            "won": sum(1 for x in pn if x > 0), "avg_delay_cost": sum(slip) / len(slip) if slip else None}


def verdict(store, started: float, now: float) -> str:
    days = (now - started) / 86400
    s = summary(store, started)
    if days < 14:
        return f"measuring ({days:.1f}/14 days, {s['closed']} closed)"
    ok = s["closed"] >= 100 and s["pnl"] > 0 and s["t"] >= 2 and s["top_share"] <= 0.5
    return "PASS" if ok else "FAIL"
