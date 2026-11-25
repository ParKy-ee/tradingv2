import unittest
from datetime import datetime, timezone

from xau_agent.journal import Journal
from xau_agent.stats import collect, setup_of, stats_text
from tests.fakes import FakeBroker


def cycle(j, cid, summary):
    ts = datetime.now(timezone.utc)
    j.save_cycle({"cycle_id": cid, "ts": ts.isoformat(), "day": ts.strftime("%Y-%m-%d"), "mode": "demo",
                  "decision": "x", "summary": summary}, {})


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.j = Journal(":memory:")
        self.b = FakeBroker()

    def test_setup_of(self):
        self.assertEqual(setup_of("Setup: PULLBACK. Context: TREND"), "PULLBACK")
        self.assertEqual(setup_of(None, "range_fade lowercase"), "UNNAMED")
        self.assertEqual(setup_of("", "RANGE_FADE at low"), "RANGE_FADE")

    def test_collect_win_loss_unfilled_and_manual_excluded(self):
        cycle(self.j, "c1", "Setup: PULLBACK")
        cycle(self.j, "c2", "Setup: PULLBACK")
        cycle(self.j, "c3", "Setup: BREAKOUT")
        for cid, t in (("c1", 1), ("c2", 2), ("c3", 3), ("manual-test", 4)):
            self.j.order(cycle_id=cid, mode="demo", side="buy", lot=0.01, sl=1, tp=2, approved=1, sent=1, ok=1,
                         ticket=t)
        self.b.deals = {1: {"closed": True, "net_profit": 12.0, "exit_reason": "tp"},
                        2: {"closed": True, "net_profit": -7.3, "exit_reason": "sl"},
                        4: {"closed": True, "net_profit": -1.0, "exit_reason": "manual"}}
        data = collect(self.j, self.b)
        self.assertEqual(data["PULLBACK"]["wins"], 1)
        self.assertEqual(data["PULLBACK"]["losses"], 1)
        self.assertAlmostEqual(data["PULLBACK"]["net"], 4.7)
        self.assertEqual(data["BREAKOUT"]["filled"], 0)  # pending never filled
        self.assertNotIn("UNNAMED", data)  # manual order excluded
        self.assertIn("PULLBACK: placed 2, filled 2, closed 1W/1L, net +4.70", stats_text(self.j, self.b))

    def test_empty(self):
        self.assertEqual(stats_text(self.j, self.b), "(no realized trades yet)")


if __name__ == "__main__":
    unittest.main()
