"""End-to-end log flow with a fake Claude client: cycle -> transcript/SQLite -> label -> export."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from xau_agent.agent import run_cycle
from xau_agent.config import RiskConfig
from xau_agent.dataset import export
from xau_agent.journal import Journal
from xau_agent.labeling import run_labeler
from xau_agent.notify import Notifier
from xau_agent.risk import RiskEngine
from xau_agent.tools import ToolExecutor
from tests.fakes import FakeBroker


def resp(stop, *blocks):
    return NS(model="claude-opus-5-5", stop_reason=stop, content=list(blocks),
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50))


class FakeClient:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class LoggingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.journal = Journal(str(self.dir / "j.sqlite"), str(self.dir / "logs"))

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def test_cycle_is_logged_labeled_and_exported(self):
        broker = FakeBroker()
        ex = ToolExecutor(broker, RiskEngine(RiskConfig(max_lot=0.05, max_risk_per_trade_pct=1.0)),
                          self.journal, Notifier(), "advisory", 260512, "", "c1")
        client = FakeClient([
            resp("tool_use", NS(type="tool_use", id="t1", name="get_market_snapshot", input={})),
            resp("tool_use", NS(type="tool_use", id="t2", name="propose_order",
                                input={"side": "buy", "lot": 0.01, "sl": 2648.20, "tp": 2652.20, "reason": "r"})),
            resp("end_turn", NS(type="text", text="BUY: test setup.")),
        ])
        summary = run_cycle(client, ex, self.journal, model="claude-opus-5-5", effort="low",
                            fallbacks="default", trigger="test")
        self.assertEqual(summary, "BUY: test setup.")
        self.assertEqual(client.calls[0]["fallbacks"], "default")
        self.assertEqual(client.calls[0]["thinking"]["display"], "summarized")

        row = self.journal.db.execute("SELECT * FROM cycles").fetchone()
        self.assertEqual(row["decision"], "buy")
        self.assertEqual(row["llm_calls"], 3)
        self.assertEqual(row["input_tokens"], 300)

        files = list((self.dir / "logs" / "cycles").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        rec = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(len(rec["messages"]), 6)  # user, asst, results, asst, results, asst
        self.assertEqual(rec["actions"][0]["status"], "advisory_only_not_sent")
        self.assertTrue(list((self.dir / "logs" / "prompts").glob("*.json")))

        # move the clock 5h forward and provide M1 bars that hit TP
        t0 = broker._q.server_epoch // 60 * 60
        broker._q.server_epoch += 5 * 3600
        broker.bars = lambda tf, n: [
            {"time": t0 + 60, "open": 2650.2, "high": 2652.5, "low": 2650.0, "close": 2652.3, "tick_volume": 1},
        ]
        self.assertEqual(run_labeler(broker, self.journal, [15, 60], 240), 1)
        label = self.journal.db.execute("SELECT * FROM labels").fetchone()
        self.assertEqual(label["status"], "complete")
        self.assertEqual(json.loads(label["orders"])[0]["sim"]["outcome"], "tp")
        self.assertEqual(run_labeler(broker, self.journal, [15, 60], 240), 0)  # not relabeled

        n_csv, n_jsonl = export(str(self.dir / "j.sqlite"), str(self.dir / "logs"), str(self.dir / "out"), False)
        self.assertEqual((n_csv, n_jsonl), (1, 1))
        csv_text = (self.dir / "out" / "cycles.csv").read_text(encoding="utf-8-sig")
        self.assertIn("tp", csv_text)
        train = json.loads((self.dir / "out" / "train.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(train["labels"]["orders"][0]["sim"]["pnl_points"], 200.0)

    def test_no_trade_cycle(self):
        broker = FakeBroker()
        ex = ToolExecutor(broker, RiskEngine(RiskConfig()), self.journal, Notifier(), "advisory", 1, "", "c2")
        client = FakeClient([resp("end_turn", NS(type="text", text="NO TRADE"))])
        run_cycle(client, ex, self.journal, model="m", effort="low", fallbacks="off", trigger="t")
        self.assertNotIn("fallbacks", client.calls[0])
        self.assertEqual(self.journal.db.execute("SELECT decision FROM cycles").fetchone()[0], "no_trade")


if __name__ == "__main__":
    unittest.main()
