"""S1: multi-outcome arbitrage on negRisk events (exactly one outcome resolves YES).
Buy-all: one YES of every outcome costs less than the $1 it pays. Sell-all: one NO of every outcome
(paid n - 1) costs less than n - 1. Candidates come from Gamma, profits from walking the CLOB books."""
from __future__ import annotations

import time

from .common import fee_rate, fetch_books, http_json, maybe_json, taker_fee


def legs_of(event: dict) -> list[dict] | None:
    """The tradable outcomes of an event, or None when the event does not qualify."""
    if not event.get("negRisk") or event.get("negRiskAugmented") or event.get("enableNegRisk") is False:
        return None
    legs = []
    for m in event.get("markets") or []:
        if m.get("closed"):
            try:                                   # an outcome that already resolved YES: the others can only lose
                if float((maybe_json(m.get("outcomePrices")) or [0])[0]) > 0.5:
                    return None
            except (TypeError, ValueError, IndexError):
                return None
            continue
        if not (m.get("active") and m.get("acceptingOrders") and m.get("enableOrderBook", True)) or m.get("negRiskOther"):
            return None
        toks = maybe_json(m.get("clobTokenIds")) or []
        if len(toks) != 2:
            return None
        legs.append({"yes": str(toks[0]), "bid": m.get("bestBid"), "ask": m.get("bestAsk"), "rate": fee_rate(m)})
    return legs if 3 <= len(legs) <= 40 else None


def prescreen(legs: list[dict]) -> list[str]:
    """Cheap test on Gamma's best prices; the books decide."""
    kinds = []
    try:
        if all(lg["ask"] for lg in legs) and sum(float(lg["ask"]) for lg in legs) < 1.0:
            kinds.append("buy_all")
        if all(lg["bid"] for lg in legs) and sum(float(lg["bid"]) for lg in legs) > 1.0:
            kinds.append("sell_all")
    except (TypeError, ValueError):
        pass
    return kinds


def walk(kind: str, legs: list[dict], books: dict, budget: float) -> tuple[float, float, float]:
    """(sets, cost, profit) of the best executable bundle under the budget, after taker fees."""
    n = len(legs)
    side = "asks" if kind == "buy_all" else "bids"
    q = []
    for lg in legs:
        lv = [list(x) for x in (books.get(lg["yes"]) or {}).get(side, [])]
        if not lv:
            return 0.0, 0.0, 0.0
        q.append(lv)
    payout = 1.0 if kind == "buy_all" else float(n - 1)
    sets = cost = profit = 0.0
    while cost < budget - 1e-9:
        unit = 0.0
        for lg, lv in zip(legs, q):
            p = lv[0][0] if kind == "buy_all" else 1.0 - lv[0][0]     # price paid per share of that leg
            unit += p + taker_fee(lg["rate"], p)
        if unit >= payout - 1e-9:
            break
        step = min(lv[0][1] for lv in q)
        step = min(step, (budget - cost) / unit)
        if step <= 1e-9:
            break
        sets += step
        cost += step * unit
        profit += step * (payout - unit)
        for lv in q:
            lv[0][1] -= step
            if lv[0][1] <= 1e-9:
                lv.pop(0)
                if not lv:
                    return sets, cost, profit
    return sets, cost, profit


class Arb:
    def __init__(self, cfg, store, get=http_json, sleep=time.sleep, clock=time.time):
        self.cfg, self.store, self.get, self.sleep, self.clock = cfg, store, get, sleep, clock

    def events(self) -> list[dict]:
        out = []
        for off in range(0, self.cfg.arb_events, 100):
            rows = self.get(f"{self.cfg.gamma}/events?active=true&closed=false&limit=100&offset={off}"
                            "&order=volume24hr&ascending=false") or []
            out += [e for e in rows if isinstance(e, dict)]
            if len(rows) < 100:
                break
        return out

    def scan(self) -> int:
        found = 0
        for ev in self.events():
            legs = legs_of(ev)
            if not legs:
                continue
            for kind in prescreen(legs):
                found += self.confirm(ev, legs, kind)
        return found

    def confirm(self, ev: dict, legs: list[dict], kind: str) -> int:
        toks = [lg["yes"] for lg in legs]
        sets, cost, profit = walk(kind, legs, fetch_books(self.cfg, toks, self.get), self.cfg.arb_budget)
        if profit <= 0:
            return 0
        self.sleep(self.cfg.arb_recheck_s)
        _, _, profit2 = walk(kind, legs, fetch_books(self.cfg, toks, self.get), self.cfg.arb_budget)
        now = self.clock()
        recent = self.store.one("SELECT 1 FROM arb WHERE event_id=? AND captured=1 AND ts>?", str(ev.get("id")), now - 3600)
        captured = int(profit2 > 0 and not recent)
        self.store.run("INSERT INTO arb(ts,event_id,title,kind,n,sets,cost,profit,recheck_profit,captured,profit_counted) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", now, str(ev.get("id")), str(ev.get("title"))[:120], kind,
                       len(legs), sets, cost, profit, profit2, captured, min(profit, profit2) if captured else 0.0)
        return 1


def summary(store, since: float) -> dict:
    r = store.one("SELECT COUNT(*) n, COALESCE(SUM(captured),0) c, COALESCE(SUM(profit_counted),0) p "
                  "FROM arb WHERE ts>=?", since)
    top = store.q("SELECT title, COUNT(*) n, SUM(profit_counted) p FROM arb WHERE ts>=? AND captured=1 "
                  "GROUP BY event_id ORDER BY p DESC LIMIT 5", since)
    return {"seen": r["n"], "captured": r["c"], "profit": float(r["p"]),
            "top": [[t["title"][:60], t["n"], round(float(t["p"]), 2)] for t in top]}


def verdict(store, started: float, now: float) -> str:
    days = (now - started) / 86400
    s = summary(store, started)
    if days < 14:
        return f"measuring ({days:.1f}/14 days, {s['captured']} captures)"
    ok = s["captured"] >= 20 and s["profit"] / max(s["captured"], 1) >= 1 and s["profit"] >= 50
    return "PASS" if ok else "FAIL"
