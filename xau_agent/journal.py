"""Persistent logs for review and training.

SQLite (journal.sqlite)
  events   - every tool call / LLM response / error (fine-grained audit trail)
  orders   - every proposal, risk decision and broker result
  cycles   - one row per decision cycle (decision, prices, tokens, latency)
  labels   - outcomes computed later (forward price moves, SL/TP simulation, realized P&L)

JSONL (logs/cycles/YYYY-MM-DD.jsonl)
  full transcript of each cycle: every message and content block sent to / received from Claude,
  including tool inputs and results (the market snapshot the model saw).
logs/prompts/<version>.json stores each system prompt + tool set once.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    cycle_id TEXT,
    mode TEXT NOT NULL,
    side TEXT,
    requested_lot REAL,
    lot REAL,
    sl REAL,
    tp REAL,
    approved INTEGER NOT NULL,
    sent INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    retcode INTEGER,
    ticket INTEGER,
    price REAL,
    reasons TEXT,
    agent_reason TEXT
);
CREATE TABLE IF NOT EXISTS day_equity (
    day TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    server_epoch INTEGER,
    mode TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    trigger TEXT,
    decision TEXT,
    summary TEXT,
    bid REAL,
    ask REAL,
    spread_points REAL,
    point REAL,
    llm_calls INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    latency_ms INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    cycle_id TEXT PRIMARY KEY,
    labeled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    forward TEXT,
    orders TEXT
);
"""

# columns added after the first release; added in place if missing
MIGRATIONS = {
    "orders": {"entry_ref": "REAL", "spread_points": "REAL", "order_type": "TEXT", "expires_epoch": "INTEGER"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y-%m-%d")


class Journal:
    def __init__(self, path: str, log_dir: str | None = None):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.log_dir = Path(log_dir) if log_dir else None

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # ---------- audit ----------
    def event(self, kind: str, payload: Any, cycle_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO events (ts, cycle_id, kind, payload) VALUES (?, ?, ?, ?)",
            (utc_now().isoformat(), cycle_id, kind, json.dumps(payload, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    def order(self, **row: Any) -> None:
        row.setdefault("ts", utc_now().isoformat())
        row.setdefault("day", utc_day())
        self._insert("orders", row)

    def _insert(self, table: str, row: dict[str, Any], replace: bool = False) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.db.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        self.db.commit()

    # ---------- risk helpers ----------
    def trades_today(self, mode: str, day: str | None = None) -> int:
        """Approved orders today in this mode (advisory counts its would-be orders too)."""
        cur = self.db.execute(
            "SELECT COUNT(*) FROM orders WHERE day = ? AND mode = ? AND approved = 1",
            (day or utc_day(), mode),
        )
        return int(cur.fetchone()[0])

    def day_start_equity(self, equity_now: float, day: str | None = None) -> float:
        day = day or utc_day()
        row = self.db.execute("SELECT equity FROM day_equity WHERE day = ?", (day,)).fetchone()
        if row:
            return float(row[0])
        self.db.execute("INSERT INTO day_equity (day, equity) VALUES (?, ?)", (day, equity_now))
        self.db.commit()
        return equity_now

    def recent_summaries(self, limit: int = 3) -> list[str]:
        rows = self.db.execute(
            "SELECT ts, decision, summary FROM cycles ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [f"[{r['ts']}] ({r['decision']}) {r['summary']}" for r in reversed(rows)]

    def pending_expiry(self, ticket: int) -> int | None:
        row = self.db.execute(
            "SELECT expires_epoch FROM orders WHERE ticket = ? AND order_type != 'market' ORDER BY id DESC LIMIT 1",
            (ticket,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ---------- cycles ----------
    def save_cycle(self, row: dict[str, Any], transcript: dict[str, Any]) -> None:
        row.setdefault("day", utc_day())
        self._insert("cycles", row, replace=True)
        if self.log_dir:
            path = self.log_dir / "cycles" / f"{row['day']}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(transcript, ensure_ascii=False, default=str) + "\n")

    def save_prompt_version(self, version: str, content: dict[str, Any]) -> None:
        if not self.log_dir:
            return
        path = self.log_dir / "prompts" / f"{version}.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- labels ----------
    def cycles_to_label(self, older_than_epoch: int) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT c.* FROM cycles c LEFT JOIN labels l ON l.cycle_id = c.cycle_id
               WHERE c.server_epoch IS NOT NULL AND c.server_epoch <= ?
                 AND (l.cycle_id IS NULL OR l.status = 'pending_realized')
               ORDER BY c.server_epoch""",
            (older_than_epoch,),
        ).fetchall()

    def orders_for_cycle(self, cycle_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM orders WHERE cycle_id = ? ORDER BY id", (cycle_id,)).fetchall()

    def save_label(self, cycle_id: str, status: str, forward: Any, orders: Any) -> None:
        self._insert(
            "labels",
            {
                "cycle_id": cycle_id,
                "labeled_at": utc_now().isoformat(),
                "status": status,
                "forward": json.dumps(forward, ensure_ascii=False),
                "orders": json.dumps(orders, ensure_ascii=False),
            },
            replace=True,
        )
