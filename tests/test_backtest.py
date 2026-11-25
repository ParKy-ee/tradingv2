import unittest

from xau_agent.backtest import Config, Engine, Order, metrics


def bar(t, o, h, l, c, spread=50):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "spread": spread}


class FillTests(unittest.TestCase):
    def eng(self, order):
        e = Engine([], Config(spread_points=50, slippage_points=10))
        e.order = order
        return e

    def test_buy_limit_fill_then_tp(self):
        e = self.eng(Order("X", "buy", "limit", 100.0, 99.0, 102.0, expires=10_000, placed=0))
        e._on_bar(bar(300, 100.6, 100.8, 99.4, 100.2))  # ask low 99.9 <= 100 -> fill at 100, SL not hit
        self.assertIsNotNone(e.pos)
        e._on_bar(bar(600, 100.2, 102.1, 100.1, 102.0))  # bid high >= TP
        t = e.trades[0]
        self.assertEqual((t.exit_reason, t.entry, t.exit), ("tp", 100.0, 102.0))
        self.assertAlmostEqual(t.r, 2.0)

    def test_fill_bar_only_sl_and_sl_pays_slippage(self):
        e = self.eng(Order("X", "buy", "limit", 100.0, 99.0, 102.0, expires=10_000, placed=0))
        e._on_bar(bar(300, 100.5, 103.0, 98.5, 99.0))  # fills and also spans TP and SL -> SL assumed
        t = e.trades[0]
        self.assertEqual(t.exit_reason, "sl")
        self.assertAlmostEqual(t.exit, 98.9)  # SL 99.0 minus 10 pts slippage

    def test_sell_stop_and_expiry(self):
        e = self.eng(Order("X", "sell", "stop", 100.0, 101.0, 98.0, expires=600, placed=0))
        e._on_bar(bar(600, 100.5, 100.6, 99.0, 99.5))  # already expired at t=600
        self.assertIsNone(e.pos)
        self.assertIsNone(e.order)

    def test_metrics(self):
        e = self.eng(Order("X", "buy", "limit", 100.0, 99.0, 102.0, expires=10_000, placed=0))
        e._on_bar(bar(300, 100.6, 100.8, 99.4, 100.2))
        e._on_bar(bar(600, 100.2, 102.1, 100.1, 102.0))
        m = metrics(e.trades)
        self.assertEqual(m["trades"], 1)
        self.assertEqual(m["exits"]["tp"], 1)
        self.assertAlmostEqual(m["net_points"], 200.0)


if __name__ == "__main__":
    unittest.main()
