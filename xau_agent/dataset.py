"""Export logs for review (CSV) and training (JSONL). No MT5 or API key needed.

    python -m xau_agent.dataset                     # -> exports/cycles.csv + exports/train.jsonl
    python -m xau_agent.dataset --all               # include cycles not labeled yet
    python -m xau_agent.dataset --db journal.sqlite --logs logs --out exports
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

CSV_COLUMNS = [
    "cycle_id", "ts", "mode", "model", "prompt_version", "decision", "bid", "ask", "spread_points",
    "side", "lot", "sl", "tp", "approved", "sent", "risk_reasons",
    "sim_outcome", "sim_pnl_points", "sim_minutes", "sim_mfe_points", "sim_mae_points",
    "realized_profit", "realized_exit",
    "fwd15_up", "fwd15_down", "fwd15_close", "fwd60_up", "fwd60_down", "fwd60_close",
    "label_status", "input_tokens", "output_tokens", "latency_ms", "summary",
]


def load_transcripts(log_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((log_dir / "cycles").glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    out[rec["cycle_id"]] = rec
    return out


def _fwd(forward: dict[str, Any] | None, key: str, field: str):
    item = (forward or {}).get(key)
    return item.get(field) if item else None


def export(db_path: str, log_dir: str, out_dir: str, include_unlabeled: bool) -> tuple[int, int]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return _export(db, log_dir, out_dir, include_unlabeled)
    finally:
        db.close()


def _export(db: sqlite3.Connection, log_dir: str, out_dir: str, include_unlabeled: bool) -> tuple[int, int]:
    rows = db.execute(
        """SELECT c.*, l.status AS label_status, l.forward, l.orders AS order_labels
           FROM cycles c LEFT JOIN labels l ON l.cycle_id = c.cycle_id ORDER BY c.ts"""
    ).fetchall()
    transcripts = load_transcripts(Path(log_dir))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    n_csv = n_jsonl = 0
    with (out / "cycles.csv").open("w", newline="", encoding="utf-8-sig") as fcsv, \
            (out / "train.jsonl").open("w", encoding="utf-8") as fjson:
        writer = csv.DictWriter(fcsv, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for c in rows:
            forward = json.loads(c["forward"]) if c["forward"] else None
            order_labels = {o["order_id"]: o for o in json.loads(c["order_labels"])} if c["order_labels"] else {}
            orders = db.execute("SELECT * FROM orders WHERE cycle_id = ? ORDER BY id", (c["cycle_id"],)).fetchall()
            o = orders[-1] if orders else None
            ol = order_labels.get(o["id"], {}) if o else {}
            sim, realized = ol.get("sim") or {}, ol.get("realized") or {}

            writer.writerow({
                **{k: c[k] for k in c.keys() if k in CSV_COLUMNS},
                "side": o["side"] if o else None,
                "lot": o["lot"] if o else None,
                "sl": o["sl"] if o else None,
                "tp": o["tp"] if o else None,
                "approved": o["approved"] if o else None,
                "sent": o["sent"] if o else None,
                "risk_reasons": o["reasons"] if o else None,
                "sim_outcome": sim.get("outcome"),
                "sim_pnl_points": sim.get("pnl_points"),
                "sim_minutes": sim.get("minutes_to_exit"),
                "sim_mfe_points": sim.get("mfe_points"),
                "sim_mae_points": sim.get("mae_points"),
                "realized_profit": realized.get("net_profit"),
                "realized_exit": realized.get("exit_reason"),
                "fwd15_up": _fwd(forward, "15m", "up_points"),
                "fwd15_down": _fwd(forward, "15m", "down_points"),
                "fwd15_close": _fwd(forward, "15m", "close_change_points"),
                "fwd60_up": _fwd(forward, "60m", "up_points"),
                "fwd60_down": _fwd(forward, "60m", "down_points"),
                "fwd60_close": _fwd(forward, "60m", "close_change_points"),
                "summary": (c["summary"] or "").replace("\n", " "),
            })
            n_csv += 1

            if c["label_status"] is None and not include_unlabeled:
                continue
            t = transcripts.get(c["cycle_id"], {})
            fjson.write(json.dumps({
                "cycle_id": c["cycle_id"],
                "ts": c["ts"],
                "mode": c["mode"],
                "model": c["model"],
                "prompt_version": c["prompt_version"],
                "decision": c["decision"],
                "backend": t.get("backend", "api"),
                "messages": t.get("messages"),      # api backend
                "events": t.get("events"),          # claude-code backend (stream-json)
                "actions": t.get("actions"),
                "orders": [dict(r) for r in orders],
                "labels": {"status": c["label_status"], "forward": forward, "orders": list(order_labels.values())},
            }, ensure_ascii=False, default=str) + "\n")
            n_jsonl += 1
    return n_csv, n_jsonl


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="journal.sqlite")
    p.add_argument("--logs", default="logs")
    p.add_argument("--out", default="exports")
    p.add_argument("--all", action="store_true", help="include cycles that are not labeled yet")
    a = p.parse_args()
    n_csv, n_jsonl = export(a.db, a.logs, a.out, a.all)
    print(f"cycles.csv: {n_csv} rows, train.jsonl: {n_jsonl} rows -> {a.out}")


if __name__ == "__main__":
    main()
