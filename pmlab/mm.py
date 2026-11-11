"""S2: market making on reward markets, simulated with public data. Quotes sit at mid ± half the reward
spread; a quote fills only when a public taker trade goes strictly through it (conservative: no queue
position is assumed). Result = spread P&L marked to mid + maker rebates + half of an estimated reward."""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone

from .common import fee_rate, fetch_books, http_json, maybe_json, rebate_rate, taker_fee


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def reward_score(v: float, dist: float, size: float) -> float:
    """Polymarket's liquidity-reward weight of one resting order: ((v - s) / v)^2 x size, where s is its distance
    from the mid and v the market's max spread. An order at the edge of the band earns far less than one at the mid."""
    if v <= 0 or dist >= v or size <= 0:
        return 0.0
    return ((v - dist) / v) ** 2 * size


def yes_terms(trade: dict, yes: str) -> tuple[str, float] | None:
    """A taker trade as ('buy'|'sell', price) on the YES token (the book is shared by YES and NO)."""
    try:
        px = float(trade.get("price"))
    except (TypeError, ValueError):
        return None
    side = trade.get("side")
    if str(trade.get("token_id")) == yes:
        return ("buy" if side == "BUY" else "sell"), px
    return ("sell" if side == "BUY" else "buy"), 1.0 - px


class MM:
    def __init__(self, cfg, store, get=http_json, clock=time.time):
        self.cfg, self.store, self.get, self.clock = cfg, store, get, clock

    # ---- market choice
    def reward_rows(self, pages: int = 10) -> list[dict]:
        out, cur = [], ""
        for _ in range(pages):
            r = self.get(f"{self.cfg.clob}/rewards/markets/current" + (f"?next_cursor={cur}" if cur else "")) or {}
            out += r.get("data") or []
            cur = r.get("next_cursor") or ""
            if not cur or cur == "LTE=":
                break
        return out

    def select(self) -> int:
        now = self.clock()
        rows = sorted(self.reward_rows(), key=lambda r: -float(r.get("total_daily_rate") or 0))
        chosen = []
        for r in rows:
            if len(chosen) >= self.cfg.mm_markets:
                break
            cond = r.get("condition_id")
            ms = self.get(f"{self.cfg.gamma}/markets?condition_ids={cond}") or []
            m = ms[0] if ms else {}
            toks = maybe_json(m.get("clobTokenIds")) or []
            if not m or m.get("closed") or not m.get("acceptingOrders") or len(toks) != 2:
                continue
            try:
                end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if end - now < 3 * 86400:
                continue
            yes = str(toks[0])
            book = fetch_books(self.cfg, [yes], self.get).get(yes) or {}
            if not book.get("bids") or not book.get("asks"):
                continue
            bid, ask = book["bids"][0][0], book["asks"][0][0]
            mid, maxs = (bid + ask) / 2, float(r.get("rewards_max_spread") or 0) / 100
            if not (0.2 <= mid <= 0.8) or maxs <= 0 or ask - bid > maxs:
                continue
            info = {"rate": fee_rate(m), "rebate": rebate_rate(m), "max_spread": maxs,
                    "size": float(r.get("rewards_min_size") or 20), "daily": float(r.get("total_daily_rate") or 0),
                    "tick": float(m.get("orderPriceMinTickSize") or 0.01), "quote": None}
            chosen.append(cond)
            if not self.store.one("SELECT 1 FROM mm_book WHERE market=?", cond):
                self.store.run("INSERT INTO mm_book VALUES(?,?,?,0,0,0,0,?,1,?,0,?)", cond, yes,
                               str(m.get("question") or "")[:120], now, mid, json.dumps(info))
            else:
                old = json.loads(self.store.one("SELECT info FROM mm_book WHERE market=?", cond)["info"])
                info["quote"] = old.get("quote")
                self.store.run("UPDATE mm_book SET active=1, info=? WHERE market=?", json.dumps(info), cond)
        if chosen:
            q = ",".join("?" * len(chosen))
            self.store.run(f"UPDATE mm_book SET active=0 WHERE market NOT IN ({q})", *chosen)
        self.store.set_meta("mm_selected", now)
        return len(chosen)

    # ---- one step per market
    def step(self) -> None:
        now = self.clock()
        if now - float(self.store.meta("mm_selected", 0)) > 86400:
            self.select()
        for b in self.store.q("SELECT * FROM mm_book WHERE settled=0 AND (active=1 OR inv != 0)"):
            self.step_market(dict(b), now)
        self.mark(now)

    def step_market(self, b: dict, now: float) -> None:
        info = json.loads(b["info"])
        if self.settle(b):
            return
        trades = (self.get(f"{self.cfg.data}/trades?condition={b['market']}&taker_only=true"
                           f"&start={int(b['last_trade_ts']) + 1}&limit=500") or {}).get("data") or []
        trades = sorted((t for t in trades if isinstance(t, dict)), key=lambda t: t.get("timestamp") or 0)
        q = info.get("quote") or {}
        inv, cash, reb, last = b["inv"], b["cash"], b["rebates"], b["last_trade_ts"]
        left = {"bid": q.get("bid_size", 0.0), "ask": q.get("ask_size", 0.0)}
        for t in trades:
            ts = float(t.get("timestamp") or 0)
            if ts <= last:
                continue
            last = ts
            yt = yes_terms(t, b["token"])
            try:
                size = float(t.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if not yt or ts < float(q.get("ts", now)):
                continue
            side, px = yt
            if side == "buy" and q.get("ask") is not None and px > q["ask"] + 1e-9 and left["ask"] > 0:
                f = min(size, left["ask"])
                left["ask"] -= f
                inv, cash = inv - f, cash + f * q["ask"]
                r = info["rebate"] * taker_fee(info["rate"], q["ask"]) * f
                reb += r
                self.store.run("INSERT INTO mm_fills VALUES(?,?,?,?,?,?,?)", ts, b["market"], "sell", q["ask"], f, r, day_of(ts))
            elif side == "sell" and q.get("bid") is not None and px < q["bid"] - 1e-9 and left["bid"] > 0:
                f = min(size, left["bid"])
                left["bid"] -= f
                inv, cash = inv + f, cash - f * q["bid"]
                r = info["rebate"] * taker_fee(info["rate"], q["bid"]) * f
                reb += r
                self.store.run("INSERT INTO mm_fills VALUES(?,?,?,?,?,?,?)", ts, b["market"], "buy", q["bid"], f, r, day_of(ts))
        book = fetch_books(self.cfg, [b["token"]], self.get).get(b["token"]) or {}
        mid = b["mid"]
        if book.get("bids") and book.get("asks"):
            mid = (book["bids"][0][0] + book["asks"][0][0]) / 2
        rew = b["rewards_est"]
        if q and q.get("ts") and b["active"]:                  # reward share earned while the last quote rested
            v, sz = info["max_spread"], info["size"]
            others = sum(reward_score(v, abs(p - mid), s) for p, s in book.get("bids", []) + book.get("asks", []))
            ours = sum(reward_score(v, abs(q[k] - mid), sz) for k in ("bid", "ask") if q.get(k) is not None)
            share = ours / (ours + others) if ours + others > 0 else 0.0
            rew += info["daily"] * share * min(now - float(q["ts"]), 3600) / 86400
        tick, half, sz = info["tick"], info["max_spread"] / 2, info["size"]
        nq = None
        if b["active"]:
            bid = math.floor((mid - half) / tick) * tick
            ask = math.ceil((mid + half) / tick) * tick
            lim = self.cfg.mm_inv_mult * sz
            nq = {"ts": now, "bid": round(bid, 4) if inv < lim and bid >= 0.01 else None,
                  "ask": round(ask, 4) if inv > -lim and ask <= 0.99 else None, "bid_size": sz, "ask_size": sz}
        info["quote"] = nq
        self.store.run("UPDATE mm_book SET inv=?, cash=?, rebates=?, rewards_est=?, last_trade_ts=?, mid=?, info=? WHERE market=?",
                       inv, cash, reb, rew, last, mid, json.dumps(info), b["market"])

    def settle(self, b: dict) -> bool:
        ms = self.get(f"{self.cfg.gamma}/markets?condition_ids={b['market']}") or []
        m = ms[0] if ms else {}
        if not m.get("closed"):
            return False
        toks = [str(t) for t in (maybe_json(m.get("clobTokenIds")) or [])]
        try:
            prices = [float(x) for x in (maybe_json(m.get("outcomePrices")) or [])]
        except (TypeError, ValueError):
            return False
        if b["token"] not in toks or len(prices) != len(toks) or max(prices, default=0) < 0.99:
            return False
        pay = prices[toks.index(b["token"])]
        self.store.run("UPDATE mm_book SET cash=cash+inv*?, inv=0, mid=?, settled=1, active=0 WHERE market=?",
                       pay, pay, b["market"])
        return True

    def mark(self, now: float) -> None:
        rows = self.store.q("SELECT * FROM mm_book")
        cash = sum(r["cash"] + r["rebates"] for r in rows)
        inv_val = sum(r["inv"] * r["mid"] for r in rows)
        rew = sum(r["rewards_est"] for r in rows)
        cap = sum(self.cfg.mm_inv_mult * json.loads(r["info"])["size"] * max(r["mid"], 1 - r["mid"])
                  for r in rows if r["active"])                 # dollars at risk, not shares
        self.store.run("INSERT INTO mm_marks VALUES(?,?,?,?,?,?)", now, day_of(now), cash, inv_val, rew, cap)


def result(row) -> float:
    return row["cash"] + row["inv_value"] + 0.5 * row["rewards_est"]


def summary(store, since: float) -> dict:
    marks = store.q("SELECT * FROM mm_marks WHERE ts>=? ORDER BY ts", since)
    if not marks:
        return {"net": 0.0, "days_up": 0, "days": 0, "dd": 0.0, "rew_share": 0.0, "fills": 0, "capital": 0.0}
    base = store.one("SELECT * FROM mm_marks WHERE ts<? ORDER BY ts DESC LIMIT 1", since)
    start = result(base) if base else 0.0
    last_by_day: dict[str, float] = {}
    for m in marks:
        last_by_day[m["day"]] = result(m)
    days, prev, up = sorted(last_by_day), start, 0
    for d in days:
        up += last_by_day[d] > prev
        prev = last_by_day[d]
    peak, dd = start, 0.0
    for m in marks:
        v = result(m)
        peak = max(peak, v)
        dd = min(dd, v - peak)
    net = result(marks[-1]) - start
    rew = 0.5 * (marks[-1]["rewards_est"] - (base["rewards_est"] if base else 0.0))
    fills = store.one("SELECT COUNT(*) n FROM mm_fills WHERE ts>=?", since)["n"]
    cap = max(m["capital"] for m in marks)
    spread = net - rew
    return {"net": net, "spread_pnl": spread, "rewards_counted": rew, "days_up": up, "days": len(days), "dd": dd, "rew_share": rew / net if net > 0 else 1.0,
            "fills": fills, "capital": cap}


def verdict(store, started: float, now: float) -> str:
    days = (now - started) / 86400
    s = summary(store, started)
    if days < 14:
        return f"measuring ({days:.1f}/14 days, {s['fills']} fills)"
    ok = s["net"] > 0 and s["days_up"] >= 9 and (-s["dd"]) < 0.2 * max(s["capital"], 1e-9) and s["rew_share"] < 0.7
    return "PASS" if ok else "FAIL"
