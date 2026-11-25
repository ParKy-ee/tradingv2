import json
import unittest

from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.labeling import simulate_pending
from xau_agent.notify import Notifier
from xau_agent.risk import OrderRequest, RiskEngine
from xau_agent.tools import ToolExecutor, sweep_pending
from tests.fakes import FakeBroker
from tests.test_risk import cfg, ctx

# market: bid 2650.00 / ask 2650.20
BUY_STOP = {"order_type": "buy_stop", "price": 2651.00, "lot": 0.01, "sl": 2649.00, "tp": 2653.00,
            "expiry_minutes": 30, "reason": "breakout"}


def pend(order_type, price, sl, tp, expiry=30):
    side = "buy" if order_type.startswith("buy") else "sell"
    return OrderRequest(side, 0.01, sl, tp, order_type=order_type, price=price, expiry_minutes=expiry)


class PendingRiskTests(unittest.TestCase):
    def test_valid_types(self):
        r = RiskEngine(cfg())
        for req in (pend("buy_stop", 2651.0, 2649.0, 2653.0), pend("buy_limit", 2649.0, 2647.0, 2651.0),
                    pend("sell_stop", 2649.0, 2651.0, 2647.0), pend("sell_limit", 2651.0, 2653.0, 2649.0)):
            d = r.check(req, ctx())
            self.assertTrue(d.approved, (req.order_type, d.reasons))

    def test_wrong_side_of_market(self):
        d = RiskEngine(cfg()).check(pend("buy_stop", 2649.0, 2647.0, 2651.0), ctx())
        self.assertFalse(d.approved)

    def test_too_far_and_bad_expiry(self):
        c = cfg(max_pending_distance_points=500, max_pending_expiry_min=60)
        self.assertFalse(RiskEngine(c).check(pend("buy_stop", 2660.0, 2658.0, 2662.0), ctx()).approved)
        self.assertFalse(RiskEngine(c).check(pend("buy_stop", 2651.0, 2649.0, 2653.0, expiry=0), ctx()).approved)
        self.assertFalse(RiskEngine(c).check(pend("buy_stop", 2651.0, 2649.0, 2653.0, expiry=120), ctx()).approved)

    def test_pending_counts_as_exposure(self):
        d = RiskEngine(cfg(max_open_positions=1)).check(pend("buy_stop", 2651.0, 2649.0, 2653.0), ctx(open_pending=1))
        self.assertFalse(d.approved)


class PendingToolTests(unittest.TestCase):
    def make(self, mode="demo"):
        self.broker = FakeBroker()
        self.journal = Journal(":memory:")
        risk = RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0))
        return ToolExecutor(self.broker, risk, self.journal, Notifier(), mode, 260512, "", "p1")

    def test_place_list_cancel(self):
        ex = self.make()
        out = json.loads(ex.execute("propose_pending_order", BUY_STOP)[0])
        self.assertEqual(out["status"], "placed", out)
        self.assertEqual(ex.decision(), "buy_stop")
        # broker-side expiry is passed so the order still expires if this PC is off
        self.assertEqual(self.broker.last_pending_expiry, self.broker._q.server_epoch + 1800)
        acc =json.loads(ex.execute("get_account_state", {})[0])
        self.assertEqual(len(acc["pending_orders"]), 1)
        self.assertEqual(acc["pending_orders"][0]["expires_server_epoch"], self.broker._q.server_epoch + 1800)
        out2 = json.loads(ex.execute("cancel_pending_order", {"ticket": out["ticket"], "reason": "x"})[0])
        self.assertEqual(out2["status"], "cancelled")

    def test_advisory_does_not_place(self):
        ex = self.make("advisory")
        out = json.loads(ex.execute("propose_pending_order", BUY_STOP)[0])
        self.assertEqual(out["status"], "advisory_only_not_sent")
        self.assertEqual(self.broker.pending_orders(), [])

    def test_sweep_expired_and_kill_switch(self):
        ex = self.make()
        ex.execute("propose_pending_order", BUY_STOP)
        self.assertEqual(sweep_pending(self.broker, self.journal, 260512), [])  # not expired yet
        self.broker._q.server_epoch += 31 * 60
        self.assertEqual(len(sweep_pending(self.broker, self.journal, 260512)), 1)
        ex2 = self.make()
        ex2.execute("propose_pending_order", BUY_STOP)
        self.assertEqual(len(sweep_pending(self.broker, self.journal, 260512, cancel_all=True)), 1)


T0 = 1_700_000_000 // 60 * 60


def bar(i, h, l, c):
    return {"time": T0 + 60 * i, "open": c, "high": h, "low": l, "close": c, "tick_volume": 1}


class PendingLabelTests(unittest.TestCase):
    def test_not_triggered(self):
        bars = [bar(1, 2650.5, 2649.8, 2650.0)]
        r = simulate_pending(bars, T0, "buy_stop", 2651.0, 2649.0, 2653.0, 0.2, 0.01, 30, 240)
        self.assertEqual(r["outcome"], "not_triggered")

    def test_triggered_then_tp(self):
        bars = [bar(1, 2650.5, 2649.8, 2650.0), bar(2, 2651.2, 2650.4, 2651.0), bar(3, 2653.5, 2651.0, 2653.2)]
        r = simulate_pending(bars, T0, "buy_stop", 2651.0, 2649.0, 2653.0, 0.2, 0.01, 30, 240)
        self.assertTrue(r["triggered"])
        self.assertEqual(r["minutes_to_trigger"], 2)
        self.assertEqual(r["outcome"], "tp")
        self.assertEqual(r["pnl_points"], 200.0)

    def test_expiry_limits_trigger_window(self):
        bars = [bar(1, 2650.5, 2649.8, 2650.0), bar(5, 2652.0, 2650.0, 2651.5)]
        r = simulate_pending(bars, T0, "buy_stop", 2651.0, 2649.0, 2653.0, 0.2, 0.01, 3, 240)
        self.assertEqual(r["outcome"], "not_triggered")


if __name__ == "__main__":
    unittest.main()
