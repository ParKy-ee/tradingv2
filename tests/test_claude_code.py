import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from xau_agent import cli
from xau_agent.agent_cc import ALLOWED_TOOLS, decision_from_journal, parse_stream, run_cycle_cc
from xau_agent.journal import Journal
from tests.fakes import FakeBroker

HERE = Path(__file__).resolve().parent


class ClaudeCodeBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.journal = Journal(str(self.dir / "j.sqlite"), str(self.dir / "logs"))

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def test_parse_stream(self):
        events, result = parse_stream('{"type":"system"}\nnoise\n{"type":"result","result":"ok"}\n')
        self.assertEqual(len(events), 2)
        self.assertEqual(result["result"], "ok")

    def test_decision_from_journal(self):
        self.journal.order(cycle_id="c9", mode="demo", side="sell", lot=0.01, sl=1, tp=0.5, approved=1, sent=1, ok=1)
        self.assertEqual(decision_from_journal(self.journal, "c9")[0], "sell")
        self.assertEqual(decision_from_journal(self.journal, "none")[0], "no_trade")

    def test_run_cycle_with_fake_cli(self):
        # a .cmd wrapper exercises Windows argument quoting the same way the real claude.cmd does
        wrapper = self.dir / "claude.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{HERE / "fake_claude.py"}" %*\r\n', encoding="ascii")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "should-be-removed"}):
            summary = run_cycle_cc(broker=FakeBroker(), journal=self.journal, mode="demo", cycle_id="cc1",
                                   trigger="test", claude_bin=str(wrapper), model="sonnet")
        self.assertIn("NO TRADE", summary)
        row = self.journal.db.execute("SELECT * FROM cycles WHERE cycle_id='cc1'").fetchone()
        self.assertIsNone(row["error"], row["error"])
        self.assertEqual(row["decision"], "no_trade")
        self.assertEqual(row["input_tokens"], 1200)
        rec = json.loads(next((self.dir / "logs" / "cycles").glob("*.jsonl")).read_text(encoding="utf-8"))
        init = rec["events"][0]
        for rule in ALLOWED_TOOLS:
            self.assertIn(rule, init["argv"])
        self.assertIn("sonnet", init["argv"])
        self.assertEqual(init["cycle_id"], "cc1")
        self.assertFalse(init["has_api_key"])

    def test_missing_cli_is_logged_as_error(self):
        run_cycle_cc(broker=FakeBroker(), journal=self.journal, mode="demo", cycle_id="cc2", trigger="t",
                     claude_bin=str(self.dir / "nope.exe"))
        row = self.journal.db.execute("SELECT * FROM cycles WHERE cycle_id='cc2'").fetchone()
        self.assertEqual(row["decision"], "error")

    def test_cli_propose_goes_through_risk_and_one_per_cycle(self):
        broker = FakeBroker()
        env = {"MODE": "demo", "JOURNAL_DB": str(self.dir / "j.sqlite"), "LOG_DIR": str(self.dir / "logs"),
               "XAU_CYCLE_ID": "cc3", "TRADING_HOURS_UTC": "", "KILL_SWITCH_FILE": str(self.dir / "STOP"), "MAX_SPREAD_POINTS": "70", "MAX_LOT": "0.05"}
        args = ["propose", "--side", "buy", "--lot", "0.01", "--sl", "2648.20", "--tp", "2652.20", "--reason", "t"]
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "MT5Broker", return_value=broker), \
                mock.patch("dotenv.load_dotenv"):
            out1, out2 = io.StringIO(), io.StringIO()
            with redirect_stdout(out1):
                cli.main(args)
            with redirect_stdout(out2):
                cli.main(args)
        self.assertEqual(json.loads(out1.getvalue())["status"], "sent", out1.getvalue())
        self.assertEqual(json.loads(out2.getvalue())["status"], "rejected")
        self.assertEqual(len(broker.sent), 1)


if __name__ == "__main__":
    unittest.main()
