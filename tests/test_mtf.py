import json
import unittest

from xau_agent import indicators
from xau_agent.broker import build_snapshot
from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.mtf import htf_bias, summarize
from xau_agent.notify import Notifier
from xau_agent.risk import OrderRequest, RiskEngine
from xau_agent.tools import SNAPSHOT_TIMEFRAMES, ToolExecutor
from tests.fakes import FakeBroker
from tests.test_risk import cfg, ctx


def series(step, n=120):
    closes = [2650 + i * step for i in range(n)]
    return [c + 0.5 for c in closes], [c - 0.5 for c in closes], closes


class TrendTests(unittest.TestCase):
    def test_trend_directions(self):
        self.assertEqual(indicators.trend(*series(0.3))["trend"], "up")
        self.assertEqual(indicators.trend(*series(-0.3))["trend"], "down")
        self.assertEqual(indicators.trend(*series(0.0))["trend"], "flat")
        self.assertIsNone(indicators.trend(*series(0.3, n=30)))

    def test_summarize_bias(self):
        s = summarize({"M1": "up", "M5": "down", "M15": "down", "M30": "down", "H1": "down"})
        self.assertEqual(s["htf_bias"], "down")
        self.assertEqual(s["down_count"], 4)
        self.assertEqual(s["line"], "M1 up | M5 down | M15 down | M30 down | H1 down")
        self.assertIsNone(summarize({"M15": "down", "M30": "flat", "H1": "down"})["htf_bias"])
        self.assertIsNone(summarize({})["htf_bias"])

    def test_snapshot_has_m30_and_alignment(self):
        snap = build_snapshot(FakeBroker(), SNAPSHOT_TIMEFRAMES)
        self.assertIn("M30", snap["timeframes"])
        self.assertEqual(snap["timeframes"]["M5"]["trend"], "up")  # fake bars rise steadily
        self.assertEqual(snap["mtf_alignment"]["htf_bias"], "up")
        self.assertEqual(htf_bias(FakeBroker()), "up")


class HtfGateTests(unittest.TestCase):
    def test_blocks_against_bias_only(self):
        r = RiskEngine(cfg())
        sell = OrderRequest("sell", 0.01, 2652.2, 2647.0)  # SL 220 pts, TP 300 pts
        buy = OrderRequest("buy", 0.01, 2648.2, 2652.2)
        d = r.check(sell, ctx(htf_bias="up"))
        self.assertFalse(d.approved)
        self.assertTrue(any("higher-timeframe" in x for x in d.reasons))
        self.assertTrue(r.check(buy, ctx(htf_bias="up")).approved)
        self.assertTrue(r.check(sell, ctx(htf_bias=None)).approved)
        self.assertTrue(RiskEngine(cfg(block_against_htf=False)).check(sell, ctx(htf_bias="up")).approved)

    def test_tool_path_uses_live_bias(self):
        ex = ToolExecutor(FakeBroker(), RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0)),
                          Journal(":memory:"), Notifier(), "demo", 260512, "", "h1")
        out = json.loads(ex.execute("propose_order", {"side": "sell", "lot": 0.01, "sl": 2652.2, "tp": 2647.0,
                                                      "reason": "fade"})[0])
        self.assertEqual(out["status"], "rejected_by_risk_engine")
        self.assertTrue(any("higher-timeframe" in x for x in out["reasons"]))


class TradesTodayTests(unittest.TestCase):
    def test_unfilled_pending_and_manual_do_not_count(self):
        broker = FakeBroker()
        j = Journal(":memory:")
        ex = ToolExecutor(broker, RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0)),
                          j, Notifier(), "demo", 260512, "", "t1")
        ex.execute("propose_pending_order", {"order_type": "buy_stop", "price": 2651.0, "lot": 0.01, "sl": 2649.0,
                                             "tp": 2653.0, "expiry_minutes": 30, "reason": "b"})
        self.assertEqual(ex._trades_today(), 1)  # still working
        broker._pending.clear()  # expired at the broker, never filled
        self.assertEqual(ex._trades_today(), 0)
        j.order(cycle_id="manual-x", mode="demo", side="buy", lot=0.01, sl=1, tp=2, approved=1, sent=1, ok=1)
        self.assertEqual(ex._trades_today(), 0)
        j.order(cycle_id="t2", mode="demo", side="buy", lot=0.01, sl=1, tp=2, approved=1, sent=1, ok=1)
        self.assertEqual(ex._trades_today(), 1)


if __name__ == "__main__":
    unittest.main()
