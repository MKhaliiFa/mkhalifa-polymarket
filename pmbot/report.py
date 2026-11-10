"""The daily Telegram report (sent at 09:01 Cairo, right after the main bot's 09:00 report)."""
from __future__ import annotations

from datetime import datetime
from html import escape


def brier(pairs) -> float | None:
    pairs = list(pairs)
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def _usd(x: float, sign: bool = True) -> str:
    s = "+" if sign and x > 0 else ("−" if x < 0 else "")
    return f"{s}${abs(x):,.2f}"


def _stats(store, since: float) -> dict:
    t = store.one(
        "SELECT COUNT(*) n, COALESCE(SUM(won),0) w, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(fee*shares),0) fees, "
        "COALESCE(AVG(edge),0) edge FROM trades WHERE status='closed' AND settled_ts>=?", since)
    cal = store.q("SELECT cal_fair_up f, cal_mid_up m, COALESCE(official_up, proxy_up) y FROM windows "
                  "WHERE settled_ts>=? AND cal_fair_up IS NOT NULL AND cal_mid_up IS NOT NULL", since)
    chk = store.one("SELECT COUNT(*) n, COALESCE(SUM(proxy_up=official_up),0) ok FROM windows "
                    "WHERE settled_ts>=? AND official_up IS NOT NULL AND proxy_up IS NOT NULL", since)
    win = store.one("SELECT COUNT(*) n FROM windows WHERE settled_ts>=?", since)
    return {"n": t["n"], "w": t["w"], "pnl": float(t["pnl"]), "fees": float(t["fees"]), "edge": float(t["edge"]),
            "bm": brier((r["f"], r["y"]) for r in cal), "bk": brier((r["m"], r["y"]) for r in cal),
            "cal_n": len(cal), "chk_n": chk["n"], "chk_ok": chk["ok"], "windows": win["n"]}


def daily_report(engine, now: float) -> str:
    cfg, st = engine.cfg, engine.store
    day = _stats(st, now - 86400)
    allt = _stats(st, 0)
    started = float(st.meta("started") or now)
    days_on = max((now - started) / 86400, 0)
    errs = st.one("SELECT COUNT(*) n FROM errors WHERE ts>=?", now - 86400)["n"]
    openn = st.one("SELECT COUNT(*) n FROM trades WHERE status='open'")["n"]
    date = datetime.fromtimestamp(now, engine.tz).strftime("%a %d %b")

    L = [f"🎯 <b>Polymarket · {escape(date)}</b> (PAPER)"]
    if day["n"]:
        L.append(f"24 h: {day['n']} trades · {day['w'] / day['n']:.0%} won · <b>{_usd(day['pnl'])}</b>"
                 f" (fees {_usd(day['fees'], False)})")
    else:
        L.append(f"24 h: No trade passed the rules ({day['windows']} windows watched)")
    if day["bm"] is not None:
        L.append(f"Model vs market: {day['bm']:.3f} vs {day['bk']:.3f} " + ("✅" if day["bm"] < day["bk"] else "❌"))
    L.append(f"Since start ({days_on:.1f} d): {allt['n']} trades · {_usd(allt['pnl'])} · "
             f"bankroll {_usd(engine.bankroll(), False)}" + (f" · open {openn}" if openn else ""))
    L.append(f"Verdict: {escape(verdict(allt, days_on))}")
    if errs:
        L.append(f"⚠️ feed errors {errs}")
    return "\n".join(L)


def verdict(s: dict, days_on: float) -> str:
    if s["n"] < 100 or days_on < 3:
        return f"too early ({s['n']}/100 trades, {days_on:.1f}/3 days)"
    win_ok = s["pnl"] > 0
    model_ok = s["bm"] is not None and s["bm"] < s["bk"]
    if win_ok and model_ok:
        return "edge looks real: ready to discuss a small live test"
    if win_ok:
        return "profitable but the model does not beat the market yet: keep watching"
    return "no edge after fees: do not go live"


def summary(store, cfg, now: float) -> dict:
    """Plain numbers for the MKhaliFa dashboard (the update timer copies them next to the bot's data)."""
    day, allt = _stats(store, now - 86400), _stats(store, 0)
    started = float(store.meta("started") or now)
    days_on = max((now - started) / 86400, 0)
    bank = cfg.start_bankroll + store.realized()
    recent = [dict(r) for r in store.q(
        "SELECT asset, side, ts, price, stake, edge, status, won, pnl, settled_ts FROM trades ORDER BY id DESC LIMIT 12")]
    curve = [dict(r) for r in store.q(
        "SELECT settled_ts ts, pnl FROM trades WHERE status='closed' ORDER BY settled_ts")]
    eq, pts = cfg.start_bankroll, []
    for r in curve:
        eq += float(r["pnl"] or 0)
        pts.append([round(float(r["ts"])), round(eq, 2)])
    return {"ts": now, "name": "Polymarket", "kind": "paper", "assets": list(cfg.assets), "days": round(days_on, 2),
            "bankroll": round(bank, 2), "start": cfg.start_bankroll, "day": day, "all": allt,
            "open": store.one("SELECT COUNT(*) n FROM trades WHERE status='open'")["n"],
            "errors_24h": store.one("SELECT COUNT(*) n FROM errors WHERE ts>=?", now - 86400)["n"],
            "verdict": verdict(allt, days_on), "recent": recent, "curve": pts[-400:]}
