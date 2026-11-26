import unittest

from xau_agent.companies import parse_view, price_stats, quarterly, ticker_of


class CompanyTests(unittest.TestCase):
    def test_ticker_from_xm_description(self):
        self.assertEqual(ticker_of("Apple Inc (AAPL.OQ)"), "AAPL")
        self.assertEqual(ticker_of("Citigroup Inc (C.N)"), "C")
        self.assertEqual(ticker_of("Walmart Inc (WMT.OQ) (WMT.OQ)"), "WMT")
        self.assertIsNone(ticker_of("no ticker here"))

    def test_price_stats(self):
        bars = [{"close": 100 + i, "high": 101 + i, "low": 99 + i} for i in range(300)]
        s = price_stats(bars)
        self.assertAlmostEqual(s["ret_1m"], 399 / 378 - 1)
        self.assertGreater(s["dist_sma200"], 0)
        self.assertEqual(s["high_52w"], 400)
        self.assertEqual(price_stats(bars[:100]), {})

    def test_quarterly_derives_q4_from_annual(self):
        def f(start, end, val, fp, form="10-Q"):
            return {"start": start, "end": end, "val": val, "fy": 2025, "fp": fp, "form": form, "filed": "2026-01-30"}
        facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            f("2025-01-01", "2025-03-31", 10, "Q1"), f("2025-04-01", "2025-06-30", 20, "Q2"),
            f("2025-07-01", "2025-09-30", 30, "Q3"), f("2025-01-01", "2025-12-31", 100, "FY", "10-K")]}}}}}
        facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"] += [  # a second year: must not trip on derived Q4
            dict(f("2026-01-01", "2026-03-31", 11, "Q1"), fy=2026), dict(f("2026-04-01", "2026-06-30", 21, "Q2"), fy=2026),
            dict(f("2026-07-01", "2026-09-30", 31, "Q3"), fy=2026),
            dict(f("2026-01-01", "2026-12-31", 110, "FY", "10-K"), fy=2026)]
        q = quarterly(facts, ["Revenues"], "USD")
        self.assertEqual(q["2026-12-31"]["value"], 47)
        self.assertEqual(len(q), 8)
        self.assertEqual(q["2025-12-31"]["value"], 40)
        self.assertEqual(q["2025-12-31"]["fp"], "Q4")

    def test_merged_tags_fill_each_period_from_the_tag_that_reports_it(self):
        from xau_agent.companies import quarterly_merged

        def f(start, end, val):
            return {"start": start, "end": end, "val": val, "fy": 2025, "fp": "Q", "form": "10-Q", "filed": "x"}
        facts = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [f("2019-01-01", "2019-03-31", 5)]}},  # old tag, old period only
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [f("2026-01-01", "2026-03-31", 9)]}}}}}
        q = quarterly_merged(facts, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"], "USD")
        self.assertEqual({k: v["value"] for k, v in q.items()}, {"2026-03-31": 9, "2019-03-31": 5})

    def test_parse_view(self):
        v = parse_view('x {"stance": "like", "p_up_3m": 140, "business": "b"} y')
        self.assertEqual((v["stance"], v["p_up_3m"]), ("like", 100))
        self.assertIsNone(parse_view('{"stance": "buy"}'))


if __name__ == "__main__":
    unittest.main()
