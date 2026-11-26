import unittest

from xau_agent.shadow import _symbol_of, parse, summarize


class ParseTests(unittest.TestCase):
    def test_parses_json_inside_text(self):
        v = parse('Here you go:\n{"verdict": "Half", "confidence": 140, "drivers": ["a","b","c","d","e"], '
                  '"risks": ["x"], "reason": "r"}')
        self.assertEqual(v["verdict"], "half")
        self.assertEqual(v["confidence"], 100)
        self.assertEqual(len(v["drivers"]), 4)

    def test_rejects_unknown_verdict(self):
        self.assertIsNone(parse('{"verdict": "approve"}'))
        self.assertIsNone(parse("no json"))


class SummaryTests(unittest.TestCase):
    def test_follow_ai_weights_rule_r(self):
        rows = [{"r": 3.0, "verdict": "take"}, {"r": -1.0, "verdict": "skip"}, {"r": -1.0, "verdict": "half"}]
        s = summarize(rows)
        self.assertEqual(s["rule_R"], 1.0)
        self.assertEqual(s["follow_ai_R"], 2.5)
        self.assertEqual(s["skip"]["avg_R"], -1.0)

    def test_hold_compare_uses_first_close_call(self):
        from xau_agent.shadow import hold_compare, parse_hold
        holds = [{"action": "hold", "open_R": 0.3}, {"action": "close", "open_R": 0.4},
                 {"action": "close", "open_R": -0.5}]
        self.assertEqual(hold_compare(-1.0, holds)["ai_exit_R"], 0.4)
        self.assertEqual(hold_compare(2.5, [{"action": "hold", "open_R": 1.0}])["ai_exit_R"], 2.5)
        self.assertEqual(parse_hold('{"action": "Hold", "p_loss": "x"}')["p_loss"], 50)
        self.assertIsNone(parse_hold('{"verdict": "take"}'))

    def test_symbol_from_cycle_id(self):
        self.assertEqual(_symbol_of("swing-1795000000"), "GOLD")
        self.assertEqual(_symbol_of("swing-BTCUSD-1795000000"), "BTCUSD")


if __name__ == "__main__":
    unittest.main()
