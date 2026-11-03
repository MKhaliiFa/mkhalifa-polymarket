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
    geo = st.meta("geoblock", "?:?")
    country, state = (geo.split(":") + ["?"])[:2]
    date = datetime.fromtimestamp(now, engine.tz).strftime("%a %d %b")
    assets = ", ".join(a.upper() for a in cfg.assets)

    L = [f"🎯 <b>Polymarket · {escape(date)}</b> (PAPER, no real money)", "─────────────"]
    L.append(f"Watched {day['windows']} windows · {escape(assets)} 15m")
    if day["n"]:
        L.append(f"Trades {day['n']} · won {day['w']} · lost {day['n'] - day['w']} "
                 f"({day['w'] / day['n']:.0%})")
        L.append(f"P&amp;L {_usd(day['pnl'])} after fees {_usd(day['fees'], False)} · avg edge {day['edge'] * 100:.1f}¢")
    else:
        L.append("No trade passed the rules in the last 24 h")
    if day["bm"] is not None:
        better = "model better ✅" if day["bm"] < day["bk"] else "market better ❌"
        L.append(f"Model vs market (Brier, lower wins): {day['bm']:.3f} vs {day['bk']:.3f} · {better}")
    if day["chk_n"]:
        L.append(f"Binance matched Polymarket's result {day['chk_ok']}/{day['chk_n']}")
    L.append("")
    L.append(f"<b>Since start</b> ({days_on:.1f} days): {allt['n']} trades"
             + (f", {allt['w'] / allt['n']:.0%} won" if allt["n"] else "")
             + f" · {_usd(allt['pnl'])} · bankroll {_usd(engine.bankroll(), False)} of {_usd(cfg.start_bankroll, False)}")
    if allt["bm"] is not None:
        L.append(f"Model vs market: {allt['bm']:.3f} vs {allt['bk']:.3f} over {allt['cal_n']} windows")
    if openn:
        L.append(f"Open now: {openn}")
    L.append("")
    L.append(f"Verdict: {escape(verdict(allt, days_on))}")
    gl = "orders blocked here (fine for paper)" if state == "blocked" else "orders allowed"
    L.append(f"Server {escape(country)}: {gl}" + (f" · feed errors {errs}" if errs else ""))
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
