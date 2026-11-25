import json
import unittest

from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.notify import Notifier
from xau_agent.reviewer import OrderReviewer, parse_verdict
from xau_agent.risk import RiskEngine
from xau_agent.tools import ToolExecutor
from tests.fakes import FakeBroker

BUY = {"side": "buy", "lot": 0.01, "sl": 2648.2, "tp": 2652.2, "reason": "pullback"}
BUY_LIMIT = {"order_type": "buy_limit", "price": 2649.0, "lot": 0.01, "sl": 2647.0, "tp": 2652.0,
             "expiry_minutes": 30, "reason": "fade"}


class FakeReviewer:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, []

    def __call__(self, proposal, reason, snapshot, stats, playbook):
        self.calls.append((proposal, reason, snapshot, stats, playbook))
        return {"model": "fake", "verdict": self.verdict, "reason": "because"}


class ReviewerTests(unittest.TestCase):
    def make(self, reviewer):
        self.broker = FakeBroker()
        self.journal = Journal(":memory:")
        return ToolExecutor(self.broker, RiskEngine(RiskConfig(news_auto=False, max_lot=0.05, max_risk_per_trade_pct=1.0)),
                            self.journal, Notifier(), "demo", 260512, "", "r1", reviewer=reviewer)

    def test_parse_verdict(self):
        self.assertEqual(parse_verdict('ok {"verdict": "REJECT", "reason": "chasing"}')["verdict"], "reject")
        self.assertIsNone(parse_verdict("no json here"))
        self.assertIsNone(parse_verdict('{"verdict": "maybe"}'))

    def test_reject_blocks_market_order(self):
        rv = FakeReviewer("reject")
        out = json.loads(self.make(rv).execute("propose_order", BUY)[0])
        self.assertEqual(out["status"], "rejected_by_reviewer")
        self.assertEqual(self.broker.sent, [])
        proposal, reason, snapshot, _, _ = rv.calls[0]
        self.assertEqual(proposal["side"], "buy")
        self.assertIn("mtf_alignment", snapshot)
        row = self.journal.db.execute("SELECT approved, sent, reasons FROM orders").fetchone()
        self.assertEqual((row["approved"], row["sent"]), (0, 0))
        self.assertIn("reviewer(fake)", row["reasons"])
        ev = self.journal.db.execute("SELECT * FROM events WHERE kind='order_review'").fetchone()
        self.assertIsNotNone(ev)

    def test_approve_sends(self):
        out = json.loads(self.make(FakeReviewer("approve")).execute("propose_order", BUY)[0])
        self.assertEqual(out["status"], "sent", out)
        self.assertEqual(len(self.broker.sent), 1)

    def test_reject_blocks_pending(self):
        out = json.loads(self.make(FakeReviewer("reject")).execute("propose_pending_order", BUY_LIMIT)[0])
        self.assertEqual(out["status"], "rejected_by_reviewer")
        self.assertEqual(self.broker.pending_orders(), [])

    def test_risk_rejection_never_reaches_reviewer(self):
        rv = FakeReviewer("approve")
        self.make(rv).execute("propose_order", {**BUY, "sl": 2650.19})  # SL too tight
        self.assertEqual(rv.calls, [])

    def test_cli_failure_is_fail_closed_by_default(self):
        r = OrderReviewer("definitely-not-a-real-binary.exe", "opus", timeout_s=5)
        v = r({"side": "buy"}, "x", {}, "", "")
        self.assertEqual(v["verdict"], "reject")
        self.assertIn("fail-closed", v["reason"])
        r.fail_open = True
        self.assertEqual(r({"side": "buy"}, "x", {}, "", "")["verdict"], "approve")


if __name__ == "__main__":
    unittest.main()
