import unittest

from xau_agent.broker import OrderResult, Position, SymbolSpec
from xau_agent.journal import Journal
from xau_agent.notify import Notifier
from xau_agent.swing import backtest
from xau_agent.swing_bot import SwingBot, signal, size_lot
from tests.fakes import FakeBroker

H4 = 14400
SPEC = SymbolSpec(point=0.01, digits=2, tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=100,
                  volume_step=0.01, stops_level=0)


def mk(closes, t0=1_700_000_000):
    return [{"time": t0 + i * H4, "open": c, "high": c + 1, "low": c - 1, "close": c} for i, c in enumerate(closes)]


class SignalTests(unittest.TestCase):
    def test_breakout_up_and_down(self):
        flat = [100.0] * 25
        self.assertEqual(signal(mk(flat + [102.5]), None)["entry"], "buy")  # > prior 20 highs (101)
        self.assertEqual(signal(mk(flat + [97.5]), None)["entry"], "sell")
        self.assertIsNone(signal(mk(flat + [100.5]), None)["entry"])

    def test_exit_long_on_10_bar_low_and_flip(self):
        closes = [100.0] * 25 + [97.5]  # close below 10-bar low (99) and 20-bar low
        s = signal(mk(closes), "buy")
        self.assertTrue(s["exit"])
        self.assertEqual(s["entry"], "sell")  # same-bar flip, as in the backtest

    def test_missed_exit_is_caught_up(self):
        closes = [100.0] * 25 + [97.5] + [100.0] * 3
        bars = mk(closes)
        s = signal(bars, "buy", entry_bar_time=bars[20]["time"])
        self.assertTrue(s["exit"])
        self.assertEqual(s["exit_bar_time"], bars[25]["time"])

    def test_signal_matches_backtest_entries(self):
        # trend up then down: every live signal on completed bars must equal the backtest's entry decision
        closes = [100 + i * 0.3 for i in range(60)] + [118 - i * 0.4 for i in range(60)]
        bars = mk(closes)
        bt = backtest(bars, "DON20_10")
        bt_entries = {(t["t0"], t["side"]) for t in bt}
        live = set()
        side = None
        entry_t = None
        for k in range(22, len(bars) - 1):
            s = signal(bars[:k + 1], side, entry_t)
            if side and s["exit"]:
                side = None
            if not side and s["entry"]:
                side, entry_t = s["entry"], bars[k]["time"]
                live.add((bars[k + 1]["time"], side))  # backtest fills at the next bar's open
        self.assertEqual(live, bt_entries)


class SizingTests(unittest.TestCase):
    def test_size_lot(self):
        # XM GOLD: 1 lot, 0.01 move = $1 -> a $50 stop costs $5000 per lot, $50 per 0.01 lot
        self.assertEqual(size_lot(9000, 0.5, 50.0, SPEC, 0.05), 0.01)  # budget $45: min lot ($50) <= 2x budget
        self.assertEqual(size_lot(9000, 0.5, 200.0, SPEC, 0.05), 0.0)  # min lot would risk $200 > $90: skip
        self.assertEqual(size_lot(100000, 0.5, 50.0, SPEC, 0.05), 0.05)  # $500 budget -> 0.1, capped at max lot


class BotStepTests(unittest.TestCase):
    def test_places_once_with_broker_stop_and_no_tp(self):
        b = FakeBroker()
        b.magic = 260513
        closes = [2650.0] * 25 + [2652.5, 2652.5]  # breakout on the last completed bar, then forming bar
        b.bars = lambda tf, n: [{"time": t["time"], "open": t["open"], "high": t["high"], "low": t["low"],
                                 "close": t["close"], "tick_volume": 1} for t in mk(closes)]
        sent = []
        b.send_order = lambda side, lot, sl, tp, comment: (sent.append((side, lot, sl, tp)),
                                                           OrderResult(ok=True, retcode=10009, comment="", ticket=77,
                                                                       price=2650.2, sent=True))[1]
        b._q.server_epoch = mk(closes)[-1]["time"] + 60  # one minute after the bar closed
        j = Journal(":memory:")
        bot = SwingBot(b, j, Notifier(), "demo", "nonexistent-STOP", 0.5, 0.05, 150, 30)
        shadows = []
        bot.shadow = lambda cid, sym: shadows.append(cid)
        bot.step()
        bot.step()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(shadows), 1)
        self.assertTrue(shadows[0].startswith("swing-"))
        side, lot, sl, tp = sent[0]
        self.assertEqual((side, tp), ("buy", 0.0))
        self.assertLess(sl, 2650.2)

    def test_second_symbol_has_its_own_ids_and_does_not_see_gold_orders(self):
        j = Journal(":memory:")
        gold, btc = FakeBroker(), FakeBroker()
        gold.symbol, btc.symbol = "GOLD", "BTCUSD"
        g = SwingBot(gold, j, Notifier(), "demo", "x", 0.5, 0.05, 150, 30)
        x = SwingBot(btc, j, Notifier(), "demo", "x", 0.5, 0.05, 150, 30)
        self.assertEqual((g.prefix, g.component), ("swing-", "swing"))
        self.assertEqual((x.prefix, x.component), ("swing-BTCUSD-", "swing_btcusd"))
        j.order(cycle_id="swing-1000", mode="demo", side="buy", requested_lot=0.01, lot=0.01, sl=1.0, tp=0.0,
                agent_reason="", entry_ref=2.0, spread_points=1, order_type="market", approved=1, sent=1, ok=1,
                ticket=5)
        j.order(cycle_id="swing-BTCUSD-1000", mode="demo", side="sell", requested_lot=0.01, lot=0.01, sl=3.0,
                tp=0.0, agent_reason="", entry_ref=2.0, spread_points=1, order_type="market", approved=1, sent=1,
                ok=1, ticket=6)
        self.assertEqual(x._entry_bar_time(6), 1000)
        self.assertEqual(g._entry_bar_time(5), 1000)


if __name__ == "__main__":
    unittest.main()
