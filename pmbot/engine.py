"""The paper bot: watch each 15-minute window, price it, paper-buy when the edge after fees is big enough,
settle at the end of the window and keep the score. It never sends an order anywhere."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import model
from .config import SYMBOLS
from .feeds import FeedError
from .report import daily_report


class Engine:
    def __init__(self, cfg, feeds, store, telegram=None, clock=time.time):
        self.cfg, self.feeds, self.store, self.tg, self.clock = cfg, feeds, store, telegram, clock
        self.tz = ZoneInfo(cfg.tz)
        self.win: dict[str, dict] = {a: {} for a in cfg.assets}
        self.closes: dict[str, list[float]] = {}
        self.t_closes = self.t_geo = self.t_official = 0.0
        if self.store.meta("started") is None:
            self.store.set_meta("started", self.clock())

    # ------------------------------------------------------------------ money
    def bankroll(self) -> float:
        return self.cfg.start_bankroll + self.store.realized()

    def free_cash(self) -> float:
        r = self.store.one("SELECT COALESCE(SUM(stake+fee*shares),0) s FROM trades WHERE status='open'")
        return self.bankroll() - float(r["s"])

    def day_start(self, now: float) -> float:
        d = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    def stopped_today(self, now: float) -> bool:
        ds = self.day_start(now)
        at_open = self.cfg.start_bankroll + self.store.realized() - self.store.realized(ds)
        return self.store.realized(ds) <= -self.cfg.daily_stop_pct * at_open

    # ------------------------------------------------------------------ main tick
    def tick(self) -> None:
        now = self.clock()
        self._safe(self._geoblock, now)
        self._safe(self._refresh_closes, now)
        start = int(now // self.cfg.window_s * self.cfg.window_s)
        for a in self.cfg.assets:
            self._safe(self._ensure_window, now, a, start)
        self._safe(self._price_and_trade, now)
        self._safe(self._settle, now)
        self._safe(self._official, now)
        self._safe(self._maybe_report, now)

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except FeedError as e:
            self.store.error(self.clock(), str(e))
        except Exception as e:  # keep running; the report shows the error count
            self.store.error(self.clock(), f"{type(e).__name__}: {e}")

    def _geoblock(self, now):
        if now - self.t_geo < 3600:
            return
        self.t_geo = now
        g = self.feeds.geoblock()
        self.store.set_meta("geoblock", f"{g['country']}:{'blocked' if g['blocked'] else 'open'}")

    def _refresh_closes(self, now):
        if now - self.t_closes < 60:
            return
        self.t_closes = now
        for a in self.cfg.assets:
            self.closes[a] = self.feeds.closes_1m(SYMBOLS[a])

    def _ensure_window(self, now, asset, start):
        w = self.win[asset]
        if w.get("start") == start and w.get("market") and w.get("strike"):
            return
        if w.get("start") != start:
            self.win[asset] = w = {"start": start, "tries": 0.0}
        if now - w.get("tries", 0.0) < 10:          # retry at most every 10 s
            return
        w["tries"] = now
        if not w.get("market"):
            w["market"] = self.feeds.window_market(asset, start, self.cfg.window_s)
        if w.get("market") and not w.get("strike"):
            c = self.feeds.window_candle(SYMBOLS[asset], start, self.cfg.window_s)
            if c:
                w["strike"] = c["open"]
                self.store.run("INSERT OR IGNORE INTO windows(slug,asset,start,strike) VALUES(?,?,?,?)",
                               w["market"]["slug"], asset, start, c["open"])

    def _price_and_trade(self, now):
        ready = {a: w for a, w in self.win.items() if w.get("market") and w.get("strike")}
        if not ready:
            return
        prices = self.feeds.prices([SYMBOLS[a] for a in ready])
        tokens = [t for w in ready.values() for t in w["market"]["tokens"].values()]
        books = self.feeds.books(tokens)
        for a, w in ready.items():
            S = prices.get(SYMBOLS[a])
            if not S:
                continue
            tau = w["start"] + self.cfg.window_s - now
            if tau <= 0:
                continue
            sig = model.sigma_per_second(self.closes.get(a) or [], self.cfg.min_sigma_1m)
            p_up = model.fair_up(S, w["strike"], tau, sig, self.cfg.basis_bps)
            up = books.get(w["market"]["tokens"]["Up"]) or {}
            dn = books.get(w["market"]["tokens"]["Down"]) or {}
            slug = w["market"]["slug"]
            if now - w.get("snap", 0.0) >= self.cfg.snap_every_s:
                w["snap"] = now
                self.store.run("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", now, a, slug, S,
                               w["strike"], tau, sig, p_up, up.get("bid"), up.get("ask"), dn.get("bid"), dn.get("ask"))
            if not w.get("cal") and tau <= 300 and up.get("bid") is not None and up.get("ask") is not None:
                w["cal"] = True   # model vs market, scored once per window five minutes before the end
                self.store.run("UPDATE windows SET cal_fair_up=?, cal_mid_up=? WHERE slug=?",
                               p_up, (up["bid"] + up["ask"]) / 2, slug)
            self._maybe_trade(now, a, w, slug, p_up, tau, up, dn)

    def _maybe_trade(self, now, asset, w, slug, p_up, tau, up, dn):
        if self.store.one("SELECT 1 FROM trades WHERE slug=?", slug) or self.stopped_today(now):
            return
        sig = model.best_signal(p_up, {"Up": up.get("ask"), "Down": dn.get("ask")}, tau, self.cfg)
        if not sig:
            return
        book = up if sig.side == "Up" else dn
        shares, stake = model.stake_for(max(self.free_cash(), 0.0), sig.ask, book.get("ask_size", 0.0),
                                        book.get("min_size", 5.0), self.cfg)
        if shares <= 0:
            return
        self.store.run("INSERT INTO trades(asset,slug,side,ts,tau,price,shares,stake,fee,fair,edge) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", asset, slug, sig.side, now, tau, sig.ask, shares, stake,
                       sig.fee, sig.fair, sig.edge)

    # ------------------------------------------------------------------ settlement
    def _settle(self, now):
        due = self.store.q("SELECT * FROM windows WHERE settled_ts IS NULL AND start+?<=?",
                           self.cfg.window_s + 5, now)
        for row in due:
            c = self.feeds.window_candle(SYMBOLS[row["asset"]], int(row["start"]), self.cfg.window_s)
            if not c or c["close_time_ms"] / 1000 > now:
                continue
            up = 1 if c["close"] >= row["strike"] else 0
            self.store.run("UPDATE windows SET close=?, proxy_up=?, settled_ts=? WHERE slug=?",
                           c["close"], up, now, row["slug"])
            for t in self.store.q("SELECT * FROM trades WHERE slug=? AND status='open'", row["slug"]):
                self._close_trade(t, up, now)

    def _close_trade(self, t, up_won: int, now: float, official: int = 0):
        won = 1 if (t["side"] == "Up") == bool(up_won) else 0
        cost = t["stake"] + t["fee"] * t["shares"]
        pnl = (t["shares"] - cost) if won else -cost
        self.store.run("UPDATE trades SET status='closed', won=?, pnl=?, settled_ts=COALESCE(settled_ts,?), "
                       "official=? WHERE id=?", won, round(pnl, 6), now, official, t["id"])

    def _official(self, now):
        """Polymarket's own result (Chainlink) replaces the Binance stand-in when it arrives."""
        if now - self.t_official < 60:
            return
        self.t_official = now
        rows = self.store.q("SELECT * FROM windows WHERE official_up IS NULL AND settled_ts IS NOT NULL "
                            "AND start>? ORDER BY start LIMIT 20", now - 3 * 3600)
        for row in rows:
            m = self.feeds.window_market(row["asset"], int(row["start"]), self.cfg.window_s)
            if not m or not m.get("winner"):
                continue
            up = 1 if m["winner"] == "Up" else 0
            self.store.run("UPDATE windows SET official_up=? WHERE slug=?", up, row["slug"])
            for t in self.store.q("SELECT * FROM trades WHERE slug=?", row["slug"]):
                self._close_trade(t, up, now, official=1)

    # ------------------------------------------------------------------ report
    def _maybe_report(self, now):
        local = datetime.fromtimestamp(now, self.tz)
        day = local.strftime("%Y-%m-%d")
        if self.store.meta("last_report_day") is None:
            self.store.set_meta("last_report_day", day)        # first report: tomorrow at 09:01
            return
        if (local.hour, local.minute) < (self.cfg.report_hour, self.cfg.report_minute):
            return
        if self.store.meta("last_report_day") == day:
            return
        self.store.set_meta("last_report_day", day)
        text = daily_report(self, now)
        if self.tg:
            self.tg.send(text)
        self.store.set_meta("last_report_text", text)


def run_forever(engine: Engine, loop_s: float) -> None:
    while True:
        t0 = time.time()
        engine.tick()
        time.sleep(max(0.2, loop_s - (time.time() - t0)))
