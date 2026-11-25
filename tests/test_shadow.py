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

    def test_symbol_from_cycle_id(self):
        self.assertEqual(_symbol_of("swing-1795000000"), "GOLD")
        self.assertEqual(_symbol_of("swing-BTCUSD-1795000000"), "BTCUSD")


if __name__ == "__main__":
    unittest.main()
