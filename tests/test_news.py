import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xau_agent import news
from xau_agent.config import RiskConfig
from xau_agent.journal import Journal
from xau_agent.notify import Notifier
from xau_agent.risk import RiskEngine
from xau_agent.tools import ToolExecutor
from tests.fakes import FakeBroker
from tests.test_risk import cfg, ctx

EVENTS = [
    {"title": "Core PCE Price Index m/m", "country": "USD", "date": "2026-11-25T08:30:00-05:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.3%"},
    {"title": "CB Consumer Confidence", "country": "USD", "date": "2026-11-24T10:00:00-05:00", "impact": "Medium",
     "forecast": "92.4", "previous": "91.7"},
    {"title": "Some Speech", "country": "USD", "date": "2026-11-24T12:00:00-05:00", "impact": "Medium"},
    {"title": "German Ifo", "country": "EUR", "date": "2026-11-24T04:00:00-05:00", "impact": "High"},
]
PCE = datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc)


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.j = Journal(":memory:")
        news.store(self.j, EVENTS)

    def test_relevance_filter(self):
        self.assertEqual([news.relevant(e) for e in EVENTS], [True, True, False, False])

    def test_blackout_around_high_impact_only(self):
        self.assertIn("Core PCE", news.blackout(self.j, PCE - timedelta(minutes=10)))
        self.assertIn("Core PCE", news.blackout(self.j, PCE + timedelta(minutes=14)))
        self.assertIsNone(news.blackout(self.j, PCE - timedelta(minutes=20)))
        # medium-impact Consumer Confidence (15:00 UTC on the 24th) is shown, not blocked
        self.assertIsNone(news.blackout(self.j, datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)))

    def test_upcoming_lists_relevant_events(self):
        up = news.upcoming(self.j, PCE - timedelta(hours=1), hours=3)
        self.assertEqual([e["title"] for e in up], ["Core PCE Price Index m/m"])
        self.assertEqual(up[0]["minutes_from_now"], 60)

    def test_stale_calendar_uses_time_window_fallback(self):
        self.j.db.execute("UPDATE news_events SET fetched_at = ?", ("2020-01-01T00:00:00+00:00",))
        # winter: 08:30 New York = 13:30 UTC; a Tuesday
        r = news.blackout(self.j, datetime(2026, 12, 1, 13, 35, tzinfo=timezone.utc))
        self.assertIn("fallback", r)
        # summer (EDT): 08:30 New York = 12:30 UTC
        self.assertIn("fallback", news.blackout(self.j, datetime(2026, 7, 7, 12, 30, tzinfo=timezone.utc)))
        self.assertIsNone(news.blackout(self.j, datetime(2026, 12, 5, 13, 35, tzinfo=timezone.utc)))  # Saturday

    def test_refresh_failure_is_logged_not_raised(self):
        with mock.patch.object(news, "fetch", side_effect=OSError("offline")):
            info = news.refresh(self.j)
        self.assertFalse(info["ok"])
        self.assertIsNotNone(self.j.db.execute("SELECT 1 FROM events WHERE kind='news_fetch'").fetchone())

    def test_risk_engine_blocks_on_news(self):
        d = RiskEngine(cfg()).check(__import__("xau_agent.risk", fromlist=["OrderRequest"]).OrderRequest(
            "buy", 0.01, 2648.2, 2652.2), ctx(news_block="news blackout: Core PCE"))
        self.assertFalse(d.approved)
        self.assertIn("news blackout: Core PCE", d.reasons)

    def test_tool_path_blocks_during_event(self):
        ex = ToolExecutor(FakeBroker(), RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0)),
                          self.j, Notifier(), "demo", 260512, "", "n1")
        with mock.patch("xau_agent.tools.utc_now", return_value=PCE - timedelta(minutes=5)):
            out = json.loads(ex.execute("propose_order", {"side": "buy", "lot": 0.01, "sl": 2648.2, "tp": 2652.2,
                                                          "reason": "x"})[0])
            acc = json.loads(ex.execute("get_account_state", {})[0])
        self.assertEqual(out["status"], "rejected_by_risk_engine")
        self.assertTrue(any("Core PCE" in r for r in out["reasons"]))
        self.assertEqual(acc["upcoming_news_3h"][0]["title"], "Core PCE Price Index m/m")


if __name__ == "__main__":
    unittest.main()
