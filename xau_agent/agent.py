"""One decision cycle: Claude reads the market through tools and may propose one order.

Every cycle is fully logged (see journal.py) so it can be reviewed and labeled later.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import anthropic

from .journal import Journal, utc_now
from .stats import stats_text
from .tools import TOOLS, ToolExecutor

SYSTEM_PROMPT = """You are an intraday XAU (gold) trading analyst operating a broker account through tools.

How you work:
- Every cycle, call get_market_snapshot and get_account_state before deciding anything.
- Base every statement on tool data only. Never invent prices, news, or indicator values.
- Most cycles the correct decision is NO TRADE. Only propose an order when the data shows a clear,
  explainable setup and the expected move comfortably exceeds the current spread.
- Every proposal needs an absolute SL and TP price on the correct side of the entry, respecting the
  limits returned by get_account_state. Size modestly; the risk engine may reduce the lot.
- If the risk engine rejects a proposal, accept the decision. Do not reshape an order just to pass it.
- Decisions happen only when a candle closes, so price can run past your trigger before the next cycle.
  When your entry is a level not reached yet (breakout or pullback), place a pending order at that
  level (propose_pending_order) with SL/TP and a short expiry instead of waiting to chase price.
- Review your pending orders every cycle and cancel any whose setup is no longer valid.
- You may close a position you opened when the original reason is clearly invalidated.
- Finish with a 1-3 sentence summary: decision (BUY/SELL/CLOSE/NO TRADE), key reason, and what
  would change your mind next cycle."""

PLAYBOOK_PATH = Path(__file__).resolve().parent.parent / "strategy" / "playbook.md"


def load_playbook() -> str:
    """Strategy playbook, re-read every cycle so it can be tuned without restarting."""
    try:
        return PLAYBOOK_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def full_system_prompt() -> str:
    pb = load_playbook()
    if not pb:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\n## Current playbook (follow it unless the data clearly contradicts it)\n" + pb


def prompt_version(system: str, extra: str = "") -> str:
    return hashlib.sha256(
        json.dumps({"system": system, "tools": TOOLS, "extra": extra}, sort_keys=True).encode()
    ).hexdigest()[:12]


PROMPT_VERSION = prompt_version(SYSTEM_PROMPT)  # base version without playbook (kept for reference)


def dump_block(block: Any) -> dict[str, Any]:
    """SDK content block -> plain dict for logging."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return block.model_dump()
    return dict(vars(block))


def run_cycle(
    client: anthropic.Anthropic,
    executor: ToolExecutor,
    journal: Journal,
    *,
    model: str,
    effort: str,
    fallbacks: str,
    trigger: str,
    thinking_display: str = "summarized",
    max_iterations: int = 8,
) -> str:
    started = time.monotonic()
    ts = utc_now()
    system_text = full_system_prompt()
    version = prompt_version(system_text)
    journal.save_prompt_version(version, {"system": system_text, "tools": TOOLS})

    # market state at decision time (used later to label the outcome)
    q = executor.broker.quote()
    point = executor.broker.spec().point

    history = "\n".join(journal.recent_summaries()) or "(no previous cycles)"
    first_user = (
        f"{trigger}\nMode: {executor.mode}.\n\nYour realized demo results per setup (last 7 days):\n"
        f"{stats_text(journal, executor.broker)}\n\nYour summaries from previous cycles:\n{history}\n\n"
        "Analyze and decide."
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": first_user}]
    log_messages: list[dict[str, Any]] = [{"role": "user", "content": first_user}]
    calls: list[dict[str, Any]] = []

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": 8000,
        "system": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        "tools": TOOLS,
        "output_config": {"effort": effort},
    }
    if thinking_display in ("summarized", "omitted"):
        params["thinking"] = {"type": "adaptive", "display": thinking_display}
    if fallbacks == "default":
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"

    summary, error = "", None
    try:
        for _ in range(max_iterations):
            t0 = time.monotonic()
            response = client.beta.messages.create(messages=messages, **params)
            usage = response.usage
            calls.append({
                "model": response.model,
                "stop_reason": response.stop_reason,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            })

            if response.stop_reason == "refusal":
                summary = f"Model declined this cycle: {getattr(response, 'stop_details', None)}"
                log_messages.append({"role": "assistant", "content": [dump_block(b) for b in response.content]})
                break

            messages.append({"role": "assistant", "content": response.content})
            log_messages.append({"role": "assistant", "content": [dump_block(b) for b in response.content]})
            texts = [b.text for b in response.content if b.type == "text"]
            if texts:
                summary = "\n".join(texts).strip()

            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                content, is_error = executor.execute(block.name, dict(block.input or {}))
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
                )
            messages.append({"role": "user", "content": results})
            log_messages.append({"role": "user", "content": results})
        else:
            summary = (summary + "\n[stopped: max iterations reached]").strip()
    except anthropic.APIError as exc:
        error = repr(exc)
        journal.event("llm_error", error, executor.cycle_id)
        summary = summary or f"Claude API error: {exc}"

    latency_ms = int((time.monotonic() - started) * 1000)
    decision = "error" if error and not executor.actions else executor.decision()
    row = {
        "cycle_id": executor.cycle_id,
        "ts": ts.isoformat(),
        "day": ts.strftime("%Y-%m-%d"),
        "server_epoch": q.server_epoch or None,
        "mode": executor.mode,
        "model": calls[-1]["model"] if calls else model,
        "prompt_version": version,
        "trigger": trigger,
        "decision": decision,
        "summary": summary,
        "bid": q.bid,
        "ask": q.ask,
        "spread_points": q.spread_points(point),
        "point": point,
        "llm_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cache_read_tokens": sum(c["cache_read_tokens"] for c in calls),
        "latency_ms": latency_ms,
        "error": error,
    }
    transcript = {
        **row,
        "params": {k: v for k, v in params.items() if k not in ("system", "tools")},
        "actions": executor.actions,
        "calls": calls,
        "messages": log_messages,
    }
    journal.save_cycle(row, transcript)
    return summary
