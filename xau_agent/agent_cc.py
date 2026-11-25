"""Decision cycle through Claude Code CLI (`claude -p`), billed to your Claude plan instead of API credits.

Claude Code may only run `python -m xau_agent.cli ...`; every order still goes through the risk engine.
The full event stream (stream-json) is stored as the cycle transcript, same layout as the API backend.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .agent import SYSTEM_PROMPT, full_system_prompt, prompt_version
from .journal import Journal, utc_now
from .stats import stats_text

PROJECT_DIR = Path(__file__).resolve().parent.parent
# Claude Code on Windows may use either its Bash or its PowerShell tool; allow only our CLI in both
ALLOWED_TOOLS = ["Bash(python -m xau_agent.cli:*)", "PowerShell(python -m xau_agent.cli:*)"]

CC_INSTRUCTIONS = """
Tools are shell commands. Run them exactly like this (working directory is already correct):
  python -m xau_agent.cli snapshot
  python -m xau_agent.cli account
  python -m xau_agent.cli propose --side buy|sell --lot <lots> --sl <price> --tp <price> --reason "<short reason>"
  python -m xau_agent.cli pending --type buy_stop|buy_limit|sell_stop|sell_limit --price <entry> --lot <lots> --sl <price> --tp <price> --expiry <minutes> --reason "<short reason>"
  python -m xau_agent.cli cancel --ticket <ticket> --reason "<short reason>"
  python -m xau_agent.cli close --ticket <ticket> --reason "<short reason>"
Each prints JSON. Do not run any other command, do not read or edit files.
"""

CC_PROMPT_VERSION = "cc-" + hashlib.sha256(
    (SYSTEM_PROMPT + CC_INSTRUCTIONS + ",".join(ALLOWED_TOOLS)).encode()
).hexdigest()[:9]


def parse_stream(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """stream-json -> (events, final result event)."""
    events, result = [], None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if ev.get("type") == "result":
            result = ev
    return events, result


def decision_from_journal(journal: Journal, cycle_id: str) -> tuple[str, list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    for o in journal.orders_for_cycle(cycle_id):
        status = "approved" if o["approved"] else "rejected_by_risk_engine"
        otype = o["order_type"] if "order_type" in o.keys() and o["order_type"] else "market"
        actions.append({"tool": "propose_order", "order_type": otype, "side": o["side"], "lot": o["lot"], "sl": o["sl"], "tp": o["tp"],
                        "status": status, "sent": bool(o["sent"]), "ok": bool(o["ok"])})
    closes = journal.db.execute(
        "SELECT payload FROM events WHERE cycle_id = ? AND kind = 'tool:close_position'", (cycle_id,)
    ).fetchall()
    for c in closes:
        actions.append({"tool": "close_position", **json.loads(c[0]).get("args", {})})
    labels = []
    for a in actions:
        if a["tool"] == "propose_order":
            name = a["side"] if a["order_type"] == "market" else a["order_type"]
            labels.append(name if a["status"] == "approved" else f"rejected_{name}")
        else:
            labels.append("close")
    return "+".join(labels) or "no_trade", actions


def run_cycle_cc(
    *,
    broker,
    journal: Journal,
    mode: str,
    cycle_id: str,
    trigger: str,
    claude_bin: str,
    model: str = "",
    timeout_s: int = 420,
    max_turns: int = 12,
) -> str:
    started = time.monotonic()
    ts = utc_now()
    q = broker.quote()
    point = broker.spec().point
    system_text = full_system_prompt()
    version = "cc-" + prompt_version(system_text, CC_INSTRUCTIONS + ",".join(ALLOWED_TOOLS))[:9]
    journal.save_prompt_version(version, {"system": system_text, "cc_instructions": CC_INSTRUCTIONS,
                                                    "allowed_tools": ALLOWED_TOOLS})

    history = "\n".join(journal.recent_summaries()) or "(no previous cycles)"
    prompt = (
        f"{system_text}\n{CC_INSTRUCTIONS}\n---\n{trigger}\nMode: {mode}.\n\n"
        f"Your realized demo results per setup (last 7 days, from MT5 deal history):\n"
        f"{stats_text(journal, broker)}\n\n"
        f"Your summaries from previous cycles:\n{history}\n\nAnalyze and decide."
    )

    env = dict(os.environ)
    env["XAU_CYCLE_ID"] = cycle_id
    # make `python` resolve to this interpreter (the project venv) inside Claude Code's shell
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.pop("ANTHROPIC_API_KEY", None)  # force the logged-in Claude plan, never API credits

    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose",
           "--max-turns", str(max_turns), "--allowedTools", *ALLOWED_TOOLS]
    if model:
        cmd += ["--model", model]

    error, events, result = None, [], None
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              cwd=PROJECT_DIR, env=env, timeout=timeout_s)
        events, result = parse_stream(proc.stdout)
        if proc.returncode != 0 or result is None or result.get("is_error"):
            msg = str((result or {}).get("result") or "")
            kind = "quota_limit" if "limit" in msg.lower() else "cli_error"
            error = f"{kind}: exit={proc.returncode} result={msg[:200]} stderr={proc.stderr.strip()[-300:]}"
        elif result.get("permission_denials"):
            # tools were blocked -> the model decided without data; must not look like a real NO TRADE
            denied = [d.get("tool_input", {}).get("command") for d in result["permission_denials"]]
            error = f"permission_denied: {denied}"
    except subprocess.TimeoutExpired:
        error = f"timeout after {timeout_s}s"
    except FileNotFoundError:
        error = f"Claude Code CLI not found: {claude_bin}"

    summary = (result or {}).get("result") or (f"[claude-code error] {error}" if error else "")
    decision, actions = decision_from_journal(journal, cycle_id)
    if error and decision == "no_trade":
        decision = "error"
    usage = (result or {}).get("usage") or {}
    row = {
        "cycle_id": cycle_id,
        "ts": ts.isoformat(),
        "day": ts.strftime("%Y-%m-%d"),
        "server_epoch": q.server_epoch or None,
        "mode": mode,
        "model": "claude-code" + (f":{model}" if model else ""),
        "prompt_version": version,
        "trigger": trigger,
        "decision": decision,
        "summary": summary,
        "bid": q.bid,
        "ask": q.ask,
        "spread_points": q.spread_points(point),
        "point": point,
        "llm_calls": (result or {}).get("num_turns"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": error,
    }
    # the per-cycle part of the prompt (system text is stored once per prompt_version) — needed for training data
    transcript = {**row, "backend": "claude-code", "user_prompt": prompt.split("\n---\n", 1)[-1],
                  "actions": actions, "events": events}
    journal.save_cycle(row, transcript)
    return summary
