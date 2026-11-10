import json
import unittest

from pmlab import arb, copy, mm
from pmlab.common import LabConfig, LabStore, taker_fee
from pmlab.runner import Lab, report_lines, summary


def book(asset, bids=(), asks=()):
    return {"asset_id": asset, "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class Router:
    """Fake HTTP: url prefix -> response (callable or value)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, body=None, timeout=8.0):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return v(url, body) if callable(v) else v
        return None


def neg_event(asks, bids=None, rate=0.0):
    ms = []
    for i, a in enumerate(asks):
        ms.append({"active": True, "closed": False, "acceptingOrders": True, "clobTokenIds": json.dumps([f"Y{i}", f"N{i}"]),
                   "bestAsk": a, "bestBid": (bids or [a - 0.01] * len(asks))[i],
                   "feesEnabled": rate > 0, "feeSchedule": {"rate": rate}})
    return {"id": "e1", "title": "Who wins?", "negRisk": True, "markets": ms}


class ArbTest(unittest.TestCase):
    def test_walk_buy_all_respects_fees_depth_and_budget(self):
        legs = [{"yes": "A", "rate": 0.0}, {"yes": "B", "rate": 0.0}, {"yes": "C", "rate": 0.0}]
        books = {"A": {"asks": [(0.30, 10), (0.40, 100)]}, "B": {"asks": [(0.30, 100)]}, "C": {"asks": [(0.30, 100)]}}
        sets, cost, profit = arb.walk("buy_all", legs, books, budget=1000)
        self.assertAlmostEqual(sets, 10)               # second A level makes the set cost 1.00: stop
        self.assertAlmostEqual(profit, 10 * 0.10)
        sets, cost, profit = arb.walk("buy_all", legs, books, budget=4.5)
        self.assertAlmostEqual(cost, 4.5)
        fee_legs = [dict(lg, rate=0.5) for lg in legs]  # fees eat the edge: 3 * 0.5*0.3*0.7 = 0.315 > 0.10
        self.assertEqual(arb.walk("buy_all", fee_legs, books, 1000)[2], 0.0)

    def test_walk_sell_all_pays_n_minus_one(self):
        legs = [{"yes": k, "rate": 0.0} for k in "ABC"]
        books = {k: {"bids": [(0.40, 50)]} for k in "ABC"}   # YES bids sum 1.20: NO costs 0.6*3=1.8, pays 2
        sets, cost, profit = arb.walk("sell_all", legs, books, 1000)
        self.assertAlmostEqual(sets, 50)
        self.assertAlmostEqual(profit, 50 * 0.20)

    def test_event_filter(self):
        self.assertIsNotNone(arb.legs_of(neg_event([0.3, 0.3, 0.3])))
        self.assertIsNone(arb.legs_of(neg_event([0.5, 0.4])))                       # too few outcomes
        self.assertIsNone(arb.legs_of({**neg_event([0.3] * 3), "negRiskAugmented": True}))
        ev = neg_event([0.3] * 4)
        ev["markets"][0]["closed"] = True                                           # a resolved leg is skipped
        self.assertEqual(len(arb.legs_of(ev)), 3)
        self.assertEqual(arb.prescreen(arb.legs_of(neg_event([0.3] * 3))), ["buy_all"])

    def test_scan_captures_only_when_the_recheck_holds(self):
        st, cfg = LabStore(":memory:"), LabConfig()
        state = {"ask": 0.30}

        def books(url, body):
            return [book(x["token_id"], asks=[(state["ask"], 100)]) for x in body]
        r = Router({"/events?": [neg_event([0.3, 0.3, 0.3])], "/books": books})

        def sleep(_):
            state["ask"] = 0.34                        # gone by the re-check
        a = arb.Arb(cfg, st, get=r, sleep=sleep, clock=lambda: 1000.0)
        self.assertEqual(a.scan(), 1)
        row = st.one("SELECT * FROM arb")
        self.assertEqual(row["captured"], 0)
        self.assertGreater(row["profit"], 0)
        state["ask"] = 0.30
        a.sleep = lambda _: None
        a.scan()
        self.assertEqual(arb.summary(st, 0)["captured"], 1)
        a.scan()                                       # same event within the hour: not counted twice
        self.assertEqual(arb.summary(st, 0)["captured"], 1)


class CopyTest(unittest.TestCase):
    def test_leader_filter_leaves_out_market_makers(self):
        rows = [{"user_id": "mm", "pnl": 5e6, "volume": 1.3e8}, {"user_id": "w1", "pnl": 4e6, "volume": 4e6},
                {"user_id": "tiny", "pnl": 900, "volume": 1000}, {"user_id": "lose", "pnl": -5, "volume": 1e5}]
        self.assertEqual([x["wallet"] for x in copy.pick_leaders(rows, 20)], ["w1"])

    def test_copy_open_leader_sell_and_resolution(self):
        st, cfg = LabStore(":memory:"), LabConfig()
        t = {"now": 1000.0}
        trades = {"rows": []}
        mkt = {"closed": False, "clobTokenIds": json.dumps(["T", "U"]), "outcomePrices": json.dumps(["0.5", "0.5"]),
               "feesEnabled": True, "feeSchedule": {"rate": 0.05}}
        r = Router({"/leaderboard": {"data": [{"user_id": "w1", "pnl": 1e5, "volume": 2e5}]},
                    "/trades?": lambda u, b: {"data": trades["rows"]},
                    "/books": lambda u, b: [book("T", bids=[(0.55, 100)], asks=[(0.60, 100)])],
                    "/markets?": lambda u, b: [mkt]})
        c = copy.Copy(cfg, st, get=r, clock=lambda: t["now"])
        c.refresh_leaders()
        trades["rows"] = [{"side": "BUY", "token_id": "T", "condition_id": "C1", "size": 50, "price": 0.5, "timestamp": 900}]
        self.assertEqual(c.poll(), 0)                  # before the leader was added: never copied
        trades["rows"] = [{"side": "BUY", "token_id": "T", "condition_id": "C1", "size": 500, "price": 0.5, "timestamp": 1001},
                          {"side": "BUY", "token_id": "T", "condition_id": "C2", "size": 10, "price": 0.5, "timestamp": 1002}]
        self.assertEqual(c.poll(), 1)                  # the $5 trade is too small
        cp = st.one("SELECT * FROM copies")
        self.assertAlmostEqual(cp["entry"], 0.60)
        self.assertAlmostEqual(cp["shares"], 10 / (0.60 + taker_fee(0.05, 0.60)))
        trades["rows"] = [{"side": "SELL", "token_id": "T", "condition_id": "C1", "size": 500, "price": 0.7, "timestamp": 1003}]
        c.poll()
        cp = st.one("SELECT * FROM copies")
        self.assertEqual(cp["how"], "leader sold")
        self.assertLess(cp["pnl"], 0)                  # bought 0.60, sold 0.55, fees
        trades["rows"] = [{"side": "BUY", "token_id": "T", "condition_id": "C1", "size": 500, "price": 0.5, "timestamp": 1004}]
        c.poll()
        mkt.update(closed=True, outcomePrices=json.dumps(["1", "0"]))
        self.assertEqual(c.resolve(), 1)
        s = copy.summary(st, 0)
        self.assertEqual(s["closed"], 2)
        self.assertGreater(s["pnl"], 0)


class MMTest(unittest.TestCase):
    def test_yes_terms_maps_no_trades(self):
        self.assertEqual(mm.yes_terms({"token_id": "Y", "side": "BUY", "price": 0.6}, "Y"), ("buy", 0.6))
        self.assertEqual(mm.yes_terms({"token_id": "N", "side": "BUY", "price": 0.4}, "Y")[0], "sell")

    def test_quotes_fill_only_through_and_settle(self):
        st, cfg = LabStore(":memory:"), LabConfig()
        cfg.mm_markets = 1
        t = {"now": 10 * 86400.0}
        trades = {"rows": []}
        mkt = {"closed": False, "acceptingOrders": True, "clobTokenIds": json.dumps(["Y", "N"]),
               "endDate": "2099-01-01T00:00:00Z", "question": "Q?", "orderPriceMinTickSize": 0.01,
               "outcomePrices": json.dumps(["0.5", "0.5"]), "feesEnabled": True, "feeSchedule": {"rate": 0.04, "rebateRate": 0.25}}
        r = Router({"/rewards/markets/current": {"data": [{"condition_id": "C", "rewards_max_spread": 4, "rewards_min_size": 20,
                                                             "total_daily_rate": 10}], "next_cursor": "LTE="},
                    "/markets?": lambda u, b: [mkt],
                    "/books": lambda u, b: [book("Y", bids=[(0.49, 100)], asks=[(0.51, 100)])],
                    "/trades?": lambda u, b: {"data": trades["rows"]}})
        m = mm.MM(cfg, st, get=r, clock=lambda: t["now"])
        m.step()                                       # selects and places the first quote: 0.48 / 0.52
        q = json.loads(st.one("SELECT info FROM mm_book")["info"])["quote"]
        self.assertEqual((q["bid"], q["ask"]), (0.48, 0.52))
        t["now"] += 60
        trades["rows"] = [{"token_id": "Y", "side": "BUY", "price": 0.52, "size": 30, "timestamp": t["now"] - 10},   # at, not through
                          {"token_id": "Y", "side": "BUY", "price": 0.53, "size": 30, "timestamp": t["now"] - 5}]   # through
        m.step()
        b = st.one("SELECT * FROM mm_book")
        self.assertAlmostEqual(b["inv"], -20)
        self.assertAlmostEqual(b["cash"], 20 * 0.52)
        self.assertGreater(b["rebates"], 0)
        self.assertGreater(b["rewards_est"], 0)
        mkt.update(closed=True, outcomePrices=json.dumps(["0", "1"]))
        t["now"] += 60
        m.step()
        b = st.one("SELECT * FROM mm_book")
        self.assertEqual((b["settled"], b["inv"]), (1, 0))
        self.assertAlmostEqual(b["cash"], 20 * 0.52)  # short YES that resolved NO keeps the premium


class RunnerTest(unittest.TestCase):
    def test_summary_and_report_lines(self):
        st = LabStore(":memory:")
        Lab(LabConfig(), st, get=Router({}), clock=lambda: 1000.0)
        s = summary(st, 2000.0)
        lines = report_lines(s)
        self.assertEqual(len(lines), 4)
        self.assertIn("measuring", lines[1])


if __name__ == "__main__":
    unittest.main()
