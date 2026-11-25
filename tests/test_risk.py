import unittest
from datetime import datetime, time, timedelta, timezone

from xau_agent.config import RiskConfig, parse_blackouts
from xau_agent.risk import OrderRequest, RiskContext, RiskEngine, floor_to_step
from tests.fakes import GOLD_SPEC

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


def ctx(**kw):
    base = dict(bid=2650.00, ask=2650.20, spec=GOLD_SPEC, equity=1000.0, day_start_equity=1000.0,
                open_positions=0, trades_today=0, now_utc=NOW)
    base.update(kw)
    return RiskContext(**base)


def cfg(**kw):
    base = dict(max_lot=0.05, max_risk_per_trade_pct=1.0, max_daily_loss_pct=2.0, max_spread_points=40,
                max_open_positions=1,
                min_sl_points=50, max_sl_points=600, min_reward_risk=1.0)
    base.update(kw)
    return RiskConfig(**base)


class RiskTests(unittest.TestCase):
    def test_valid_buy_approved(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx())
        self.assertTrue(d.approved, d.reasons)
        self.assertEqual(d.lot, 0.01)
        self.assertEqual(d.reward_risk, 1.0)

    def test_missing_sl_rejected(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 0, 2652), ctx())
        self.assertFalse(d.approved)

    def test_sl_wrong_side_rejected(self):
        d = RiskEngine(cfg()).check(OrderRequest("sell", 0.01, 2648.0, 2645.0), ctx())
        self.assertFalse(d.approved)

    def test_wide_spread_rejected(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 2648.0, 2653.0), ctx(ask=2650.60))
        self.assertFalse(d.approved)
        self.assertTrue(any("spread" in r for r in d.reasons))

    def test_lot_reduced_to_risk_budget(self):
        # SL 2.00 away from ask -> $200 per lot; 0.5% of $1000 = $5 budget
        d = RiskEngine(cfg(max_risk_per_trade_pct=0.5)).check(OrderRequest("buy", 0.05, 2648.20, 2652.20), ctx())
        self.assertTrue(d.approved, d.reasons)
        self.assertEqual(d.lot, 0.02)  # $5 budget / $200 per lot = 0.025 -> floor 0.02
        self.assertLessEqual(d.risk_money, 5.0)

    def test_lot_capped_by_max_lot(self):
        d = RiskEngine(cfg(max_lot=0.01)).check(OrderRequest("buy", 1.0, 2648.20, 2652.20), ctx())
        self.assertEqual(d.lot, 0.01)

    def test_budget_below_min_lot_rejected(self):
        d = RiskEngine(cfg(max_risk_per_trade_pct=0.1)).check(OrderRequest("buy", 0.01, 2645.20, 2655.20), ctx())
        self.assertFalse(d.approved)

    def test_daily_loss_blocks(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx(equity=975.0))
        self.assertFalse(d.approved)

    def test_kill_switch_blocks(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx(kill_switch=True))
        self.assertFalse(d.approved)

    def test_low_reward_risk_rejected(self):
        d = RiskEngine(cfg()).check(OrderRequest("buy", 0.01, 2648.20, 2651.00), ctx())
        self.assertFalse(d.approved)

    def test_trading_hours(self):
        c = cfg(trading_hours_utc=(time(12, 0), time(20, 0)))
        d = RiskEngine(c).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx())
        self.assertFalse(d.approved)

    def test_news_blackout(self):
        c = cfg(news_blackouts=parse_blackouts("2026-11-23T10:15Z/30"))
        d = RiskEngine(c).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx())
        self.assertFalse(d.approved)
        d2 = RiskEngine(c).check(OrderRequest("buy", 0.01, 2648.20, 2652.20), ctx(now_utc=NOW - timedelta(hours=1)))
        self.assertTrue(d2.approved, d2.reasons)

    def test_floor_to_step(self):
        self.assertEqual(floor_to_step(0.029999, 0.01), 0.02)
        self.assertEqual(floor_to_step(0.03, 0.01), 0.03)


class MinStopAtrTests(unittest.TestCase):
    def test_min_sl_atr(self):
        from xau_agent.risk import OrderRequest, RiskEngine
        r = RiskEngine(cfg(max_sl_points=2000))
        tight = OrderRequest("buy", 0.01, 2647.2, 2655.0)  # SL 300 pts from ask 2650.20
        d = r.check(tight, ctx(atr_m5=8.0))  # 0.6 x 8 = 4.8 -> 480 pts minimum
        self.assertFalse(d.approved)
        self.assertTrue(any("M5 ATR" in x for x in d.reasons))
        self.assertTrue(r.check(OrderRequest("buy", 0.01, 2645.2, 2658.0), ctx(atr_m5=8.0)).approved)
        self.assertTrue(r.check(tight, ctx(atr_m5=None)).approved)  # no ATR -> rule skipped


if __name__ == "__main__":
    unittest.main()
