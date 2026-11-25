import unittest

from xau_agent.labeling import forward_labels, simulate_order

T0 = 1_700_000_000 // 60 * 60  # decision minute
P = 0.01


def bar(i, o, h, l, c):
    return {"time": T0 + 60 * i, "open": o, "high": h, "low": l, "close": c, "tick_volume": 1}


class LabelingTests(unittest.TestCase):
    def test_buy_hits_tp(self):
        bars = [bar(0, 2650, 2650.5, 2649.9, 2650.2),  # decision bar, ignored
                bar(1, 2650.2, 2651.0, 2649.8, 2650.9),
                bar(2, 2650.9, 2652.5, 2650.5, 2652.3)]
        r = simulate_order(bars, T0 + 10, "buy", 2650.20, 2648.20, 2652.20, 0.20, P, 60)
        self.assertEqual(r["outcome"], "tp")
        self.assertEqual(r["pnl_points"], 200.0)
        self.assertEqual(r["minutes_to_exit"], 2)

    def test_buy_same_bar_counts_as_sl(self):
        bars = [bar(1, 2650, 2653, 2648, 2650)]
        r = simulate_order(bars, T0, "buy", 2650.20, 2648.20, 2652.20, 0.20, P, 60)
        self.assertEqual(r["outcome"], "sl")
        self.assertTrue(r["same_bar_sl_tp"])
        self.assertEqual(r["pnl_points"], -200.0)

    def test_sell_uses_ask_for_exit(self):
        # bid low 2647.85 -> ask low 2648.05: TP at 2648.00 NOT reached
        bars = [bar(1, 2650, 2650.1, 2647.85, 2648.2)]
        r = simulate_order(bars, T0, "sell", 2650.00, 2652.00, 2648.00, 0.20, P, 1)
        self.assertEqual(r["outcome"], "timeout")
        self.assertAlmostEqual(r["exit_price"], 2648.40)
        self.assertEqual(r["pnl_points"], 160.0)

    def test_no_data(self):
        self.assertEqual(simulate_order([], T0, "buy", 1, 0.5, 2, 0, P, 60)["outcome"], "no_data")

    def test_forward_labels(self):
        bars = [bar(1, 2650, 2651, 2649, 2650.5), bar(2, 2650.5, 2652, 2650, 2651.5)]
        f = forward_labels(bars, T0, 2650.0, P, [1, 15])
        self.assertEqual(f["1m"], {"up_points": 100.0, "down_points": 100.0, "close_change_points": 50.0, "bars": 1})
        self.assertEqual(f["15m"]["up_points"], 200.0)
        self.assertEqual(f["15m"]["close_change_points"], 150.0)


if __name__ == "__main__":
    unittest.main()
