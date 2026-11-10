import unittest

from pmbot import model
from pmbot.config import Config
from pmbot.engine import Engine
from pmbot.feeds import parse_book, parse_market
from pmbot.report import daily_report, verdict
from pmbot.store import Store

W = 900
T0 = 1_780_000_200  # a window start (multiple of 900), Sunday 2026-05-28 area


def cfg(**kw):
    c = Config()
    c.assets = ["btc"]
    c.data_dir = ":memory:"
    c.tg_token = c.tg_chat = ""
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class FakeFeeds:
    def __init__(self):
        self.price = 100_000.0
        self.strike = 100_000.0
        self.close = None
        self.up_ask, self.dn_ask = 0.50, 0.52
        self.winner = None
        self.calls = 0

    def geoblock(self):
        return {"blocked": True, "country": "DE"}

    def closes_1m(self, symbol, n=61):
        return [100_000 * (1 + 0.0005 * ((i % 2) * 2 - 1)) for i in range(60)]

    def window_market(self, asset, start, window_s):
        return {"slug": f"{asset}-updown-15m-{start}", "tokens": {"Up": "U", "Down": "D"},
                "closed": self.winner is not None, "winner": self.winner}

    def window_candle(self, symbol, start, window_s):
        return {"open": self.strike, "close": self.close if self.close is not None else self.price,
                "close_time_ms": (start + window_s) * 1000 - 1}

    def prices(self, symbols):
        return {s: self.price for s in symbols}

    def books(self, tokens):
        return {"U": {"bid": self.up_ask - 0.01, "ask": self.up_ask, "ask_size": 500, "min_size": 5},
                "D": {"bid": self.dn_ask - 0.01, "ask": self.dn_ask, "ask_size": 500, "min_size": 5}}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class TG:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class ModelTests(unittest.TestCase):
    def test_fair_at_strike_is_half(self):
        self.assertAlmostEqual(model.fair_up(100, 100, 300, 0.0001), 0.5, places=6)

    def test_fair_moves_with_price_and_time(self):
        s = 0.0005 / 60 ** 0.5
        high = model.fair_up(100.2, 100, 600, s)
        late = model.fair_up(100.2, 100, 30, s)
        self.assertGreater(high, 0.5)
        self.assertGreater(late, high)            # same lead, less time left -> more certain
        self.assertLess(model.fair_up(99.8, 100, 600, s), 0.5)

    def test_fee_matches_docs(self):
        # docs: 100 crypto shares at $0.50 cost $1.75 in fees
        self.assertAlmostEqual(100 * model.taker_fee(0.5, 0.07), 1.75)

    def test_sigma_floor(self):
        self.assertAlmostEqual(model.sigma_per_second([100] * 61, 0.0004), 0.0004 / 60 ** 0.5)

    def test_signal_rules(self):
        c = cfg()
        sig = model.best_signal(0.70, {"Up": 0.60, "Down": 0.42}, 120, c)
        self.assertEqual(sig.side, "Up")
        self.assertAlmostEqual(sig.edge, 0.70 - 0.60 - 0.07 * 0.6 * 0.4)
        self.assertIsNone(model.best_signal(0.70, {"Up": 0.66, "Down": 0.36}, 120, c))   # edge too small
        self.assertIsNone(model.best_signal(0.99, {"Up": 0.90, "Down": 0.12}, 120, c))   # outside 0.15-0.85
        self.assertIsNone(model.best_signal(0.70, {"Up": 0.60, "Down": 0.42}, 5, c))     # too late

    def test_stake(self):
        c = cfg()
        shares, stake = model.stake_for(100, 0.5, 1000, 5, c)
        self.assertEqual((shares, stake), (6.0, 3.0))
        self.assertEqual(model.stake_for(100, 0.5, 4, 5, c), (0.0, 0.0))   # book too thin for min size


class ParseTests(unittest.TestCase):
    def test_market_and_book(self):
        m = parse_market({"outcomes": '["Up","Down"]', "clobTokenIds": '["11","22"]', "closed": True,
                          "outcomePrices": '["0","1"]'}, "btc-updown-15m-1")
        self.assertEqual(m["tokens"], {"Up": "11", "Down": "22"})
        self.assertEqual(m["winner"], "Down")
        b = parse_book({"bids": [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "3"}],
                        "asks": [{"price": "0.60", "size": "9"}, {"price": "0.55", "size": "7"}],
                        "min_order_size": "5"})
        self.assertEqual((b["bid"], b["ask"], b["ask_size"]), (0.45, 0.55, 7.0))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.f = FakeFeeds()
        self.clock = Clock(T0 + 600)          # 5 minutes left
        self.tg = TG()
        self.e = Engine(cfg(), self.f, Store(":memory:"), self.tg, self.clock)

    def test_trade_settle_and_official_fix(self):
        self.f.price = 100_150.0               # well above the strike: Up worth far more than 0.50
        self.e.tick()
        self.e.tick()                          # second tick (after 10 s retry window not needed: already ready)
        t = self.e.store.one("SELECT * FROM trades")
        self.assertIsNotNone(t)
        self.assertEqual(t["side"], "Up")
        self.e.tick()
        self.assertEqual(self.e.store.one("SELECT COUNT(*) n FROM trades")["n"], 1)   # one trade per window
        # end of window, Binance says Up
        self.f.close = 100_200.0
        self.clock.t = T0 + W + 6
        self.e.tick()
        t = self.e.store.one("SELECT * FROM trades")
        self.assertEqual((t["status"], t["won"]), ("closed", 1))
        self.assertAlmostEqual(t["pnl"], t["shares"] - t["stake"] - t["fee"] * t["shares"], places=6)
        # Polymarket's official result says Down: the trade is re-scored as a loss
        self.f.winner = "Down"
        self.clock.t += 61
        self.e.tick()
        t = self.e.store.one("SELECT * FROM trades")
        self.assertEqual((t["won"], t["official"]), (0, 1))
        self.assertLess(t["pnl"], 0)
        self.assertAlmostEqual(self.e.bankroll(), 100 + t["pnl"], places=6)

    def test_no_trade_without_edge(self):
        self.f.up_ask, self.f.dn_ask = 0.50, 0.50
        self.e.tick()
        self.assertEqual(self.e.store.one("SELECT COUNT(*) n FROM trades")["n"], 0)

    def test_daily_stop(self):
        st = self.e.store
        st.run("INSERT INTO trades(asset,slug,side,ts,tau,price,shares,stake,fee,fair,edge,status,won,pnl,settled_ts) "
               "VALUES('btc','x','Up',?,1,0.5,1,1,0,0.6,0.1,'closed',0,-11,?)", self.clock.t, self.clock.t)
        self.assertTrue(self.e.stopped_today(self.clock.t))
        self.f.price = 100_150.0
        self.e.tick()
        self.assertEqual(st.one("SELECT COUNT(*) n FROM trades WHERE slug!='x'")["n"], 0)

    def test_report_sent_once_at_0901_cairo(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Africa/Cairo")
        d0 = datetime(2026, 11, 4, 8, 0, tzinfo=tz).timestamp()
        self.clock.t = d0
        self.e.tick()                                   # first day: remembers today, no report
        self.clock.t = datetime(2026, 11, 5, 9, 0, 30, tzinfo=tz).timestamp()
        self.e.tick()
        self.assertEqual(self.tg.sent, [])              # 09:00 is the main bot's minute
        self.clock.t = datetime(2026, 11, 5, 9, 1, 5, tzinfo=tz).timestamp()
        self.e.tick()
        self.e.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("PAPER", self.tg.sent[0])
        self.assertLessEqual(len(self.tg.sent[0].splitlines()), 6)

    def test_report_text_and_verdict(self):
        txt = daily_report(self.e, self.clock.t)
        self.assertIn("No trade passed", txt)
        self.assertIn("too early", verdict({"n": 5, "pnl": 1, "bm": None, "bk": None}, 0.2))
        self.assertIn("no edge", verdict({"n": 150, "pnl": -3, "bm": 0.2, "bk": 0.19}, 4))
        self.assertIn("edge looks real", verdict({"n": 150, "pnl": 3, "bm": 0.18, "bk": 0.19}, 4))


if __name__ == "__main__":
    unittest.main()


class SummaryTest(unittest.TestCase):
    def test_summary_numbers(self):
        from pmbot.config import Config
        from pmbot.report import summary
        from pmbot.store import Store
        st = Store(":memory:")
        st.run("INSERT INTO trades(asset,slug,side,ts,tau,price,shares,stake,fee,fair,edge,status,won,pnl,settled_ts) "
               "VALUES('btc','s','up',1,1,0.5,10,5,0.01,0.6,0.1,'closed',1,4.9,100)")
        s = summary(st, Config(), 200.0)
        self.assertEqual(s["all"]["n"], 1)
        self.assertEqual(s["curve"][-1][1], round(Config().start_bankroll + 4.9, 2))
