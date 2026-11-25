import json
import unittest

from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.notify import Notifier
from xau_agent.risk import RiskEngine
from xau_agent.tools import ToolExecutor, sweep_pending
from tests.fakes import FakeBroker

# market bid 2650.00 / ask 2650.20 (fake); sell_limit above market, TP below
SELL_LIMIT = {"order_type": "sell_limit", "price": 2651.0, "lot": 0.01, "sl": 2653.0, "tp": 2648.0,
              "expiry_minutes": 30, "reason": "pullback"}


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBroker()
        self.j = Journal(":memory:")
        ex = ToolExecutor(self.b, RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0,
                                                        block_against_htf=False)),
                          self.j, Notifier(), "demo", 260512, "", "s1")
        out = json.loads(ex.execute("propose_pending_order", SELL_LIMIT)[0])
        self.assertEqual(out["status"], "placed", out)

    def reasons(self):
        rows = self.j.db.execute("SELECT payload FROM events WHERE kind='pending_cancelled'").fetchall()
        return [json.loads(r["payload"])["reason"] for r in rows]

    def test_keeps_order_while_valid(self):
        self.assertEqual(sweep_pending(self.b, self.j, 260512, max_distance_points=1500), [])
        self.assertEqual(len(self.b.pending_orders()), 1)

    def test_cancels_when_tp_traded_before_fill(self):
        self.b._q.bid, self.b._q.ask = 2647.5, 2647.7  # sell's TP 2648 already reached (ask <= TP)
        self.assertEqual(len(sweep_pending(self.b, self.j, 260512, max_distance_points=1500)), 1)
        self.assertEqual(self.reasons(), ["tp_passed"])

    def test_cancels_when_price_runs_too_far(self):
        self.b._q.bid, self.b._q.ask = 2648.6, 2648.8  # 220 pts from the entry, TP not reached yet
        self.assertEqual(sweep_pending(self.b, self.j, 260512, max_distance_points=1500), [])
        self.assertEqual(len(sweep_pending(self.b, self.j, 260512, max_distance_points=200)), 1)
        self.assertEqual(self.reasons(), ["too_far"])


if __name__ == "__main__":
    unittest.main()
