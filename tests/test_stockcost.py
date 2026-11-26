import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from xau_agent import stockcost


class StockCostTests(unittest.TestCase):
    def test_us_open_follows_new_york_dst(self):
        self.assertEqual(stockcost.us_open_utc(datetime(2026, 11, 30, 3, tzinfo=timezone.utc)).hour, 14)
        self.assertEqual(stockcost.us_open_utc(datetime(2026, 7, 1, 3, tzinfo=timezone.utc)).hour, 13)

    def test_report_buckets_by_minutes_since_open(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.csv"
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["utc", "symbol", "bid", "ask", "spread_bp", "min_since_open"])
                w.writerow(["2026-11-30T14:35:00", "Apple", 1, 1, 20.0, 5])
                w.writerow(["2026-11-30T16:00:00", "Apple", 1, 1, 4.0, 90])
            with mock.patch.object(stockcost, "CSV", p), mock.patch.object(stockcost, "PROBES", Path(d) / "none.csv"), \
                    mock.patch("builtins.print") as pr:
                stockcost.report()
            out = "\n".join(str(c.args[0]) for c in pr.call_args_list)
            self.assertIn("0.200%", out)  # first 15 min: 20 bp
            self.assertIn("0.040%", out)  # 1h-5.5h: 4 bp


if __name__ == "__main__":
    unittest.main()
