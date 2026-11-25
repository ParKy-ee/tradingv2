"""Second opinion on every order: a stronger model (default Opus) reviews the proposal before it is sent.

Runs only after the deterministic risk engine already approved the order, so it can only make trading
stricter, never looser. Uses the Claude Code CLI on the user's plan (no API credits), with no tools.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

REVIEW_PROMPT = """You are a senior XAU (gold) intraday risk reviewer. A junior trading agent wants to place the
order below on a DEMO account. The deterministic risk engine already approved size, SL/TP distances and limits.
Your job: decide if the trade idea itself is sound given the market data and the playbook. Be strict but fair:
reject trades that fight the multi-timeframe alignment, chase price, sit in the middle of a range, ignore the
playbook filters, or repeat a setup that keeps losing without a clear difference this time. Approve clean,
playbook-conforming setups. Base every statement on the data below only.

Reply with ONLY one JSON object, no other text:
{"verdict": "approve" or "reject", "reason": "<one or two sentences>"}

## Proposed order
{proposal}

## Agent's stated reason
{reason}

## Realized results per setup (demo)
{stats}

## Playbook
{playbook}

## Market snapshot (bar times are broker server time)
{snapshot}
"""


def parse_verdict(text: str) -> dict[str, str] | None:
    for m in re.finditer(r"\{.*?\}", text or "", re.S):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        v = str(obj.get("verdict", "")).strip().lower()
        if v in ("approve", "reject"):
            return {"verdict": v, "reason": str(obj.get("reason", "")).strip()[:500]}
    return None


class OrderReviewer:
    def __init__(self, claude_bin: str, model: str = "opus", timeout_s: int = 90, fail_open: bool = False):
        self.claude_bin = claude_bin
        self.model = model
        self.timeout_s = timeout_s
        self.fail_open = fail_open

    def build_prompt(self, proposal: dict[str, Any], reason: str, snapshot: dict[str, Any], stats: str,
                     playbook: str) -> str:
        return (REVIEW_PROMPT
                .replace("{proposal}", json.dumps(proposal))
                .replace("{reason}", reason or "(none)")
                .replace("{stats}", stats or "(none)")
                .replace("{playbook}", playbook or "(none)")
                .replace("{snapshot}", json.dumps(snapshot, separators=(",", ":"))))

    def __call__(self, proposal: dict[str, Any], reason: str, snapshot: dict[str, Any], stats: str,
                 playbook: str) -> dict[str, Any]:
        prompt = self.build_prompt(proposal, reason, snapshot, stats, playbook)
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # plan billing only
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):  # we may run inside the agent's own Claude Code shell
            env.pop(k, None)
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--max-turns", "1", "--model", self.model]
        t0 = time.monotonic()
        out: dict[str, Any] = {"model": self.model}
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  env=env, timeout=self.timeout_s)
            res = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
            text = str(res.get("result") or "")
            verdict = parse_verdict(text)
            if proc.returncode != 0 or res.get("is_error") or not verdict:
                out["error"] = f"exit={proc.returncode} result={text[:200]} stderr={proc.stderr.strip()[-200:]}"
            else:
                out.update(verdict)
            out["usage"] = res.get("usage")
        except subprocess.TimeoutExpired:
            out["error"] = f"timeout after {self.timeout_s}s"
        except (FileNotFoundError, OSError) as exc:
            out["error"] = f"reviewer CLI failed: {exc}"
        out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        if "verdict" not in out:
            out["verdict"] = "approve" if self.fail_open else "reject"
            out["reason"] = f"reviewer unavailable ({'fail-open' if self.fail_open else 'fail-closed'}): {out['error']}"
        return out
