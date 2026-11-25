import json
import unittest

from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.notify import Notifier
from xau_agent.risk import RiskEngine
from xau_agent.tools import ToolExecutor
from tests.fakes import FakeBroker

RISK = RiskConfig(news_auto=False, max_lot=0.05, max_risk_per_trade_pct=1.0, min_sl_points=50, max_sl_points=600)
ORDER = {"side": "buy", "lot": 0.01, "sl": 2648.20, "tp": 2652.20, "reason": "test"}


def make(mode):
    broker = FakeBroker()
    ex = ToolExecutor(broker, RiskEngine(RISK), Journal(":memory:"), Notifier(), mode, 260512, "", "c1")
    return broker, ex


class ToolTests(unittest.TestCase):
    def test_snapshot_and_account(self):
        _, ex = make("advisory")
        snap, err = ex.execute("get_market_snapshot", {})
        self.assertFalse(err, snap)
        self.assertIn("M5", json.loads(snap)["timeframes"])
        acc, err = ex.execute("get_account_state", {})
        self.assertFalse(err, acc)
        self.assertEqual(json.loads(acc)["mode"], "advisory")

    def test_advisory_never_sends(self):
        broker, ex = make("advisory")
        out, _ = ex.execute("propose_order", ORDER)
        self.assertEqual(json.loads(out)["status"], "advisory_only_not_sent")
        self.assertEqual(broker.sent, [])
        self.assertEqual(len(broker.checked), 1)

    def test_demo_sends_and_one_per_cycle(self):
        broker, ex = make("demo")
        out, _ = ex.execute("propose_order", ORDER)
        self.assertEqual(json.loads(out)["status"], "sent")
        self.assertEqual(len(broker.sent), 1)
        out2, _ = ex.execute("propose_order", ORDER)
        self.assertEqual(json.loads(out2)["status"], "rejected")
        self.assertEqual(len(broker.sent), 1)

    def test_risk_rejection_not_sent(self):
        broker, ex = make("demo")
        bad = dict(ORDER, sl=2651.0)  # SL above entry for a buy
        out, _ = ex.execute("propose_order", bad)
        self.assertEqual(json.loads(out)["status"], "rejected_by_risk_engine")
        self.assertEqual(broker.sent, [])

    def test_close_only_own_positions(self):
        _, ex = make("demo")
        out, _ = ex.execute("close_position", {"ticket": 999, "reason": "x"})
        self.assertEqual(json.loads(out)["status"], "rejected")

    def test_unknown_tool(self):
        _, ex = make("advisory")
        _, err = ex.execute("rm_rf", {})
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
