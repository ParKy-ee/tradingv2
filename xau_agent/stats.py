"""Realized results per setup, fed back to the agent each cycle.

Setup names come from the agent's own cycle summary (playbook asks it to name RANGE_FADE / BREAKOUT /
PULLBACK). Outcomes come from MT5 deal history, so they are real demo fills, not simulations.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

SETUPS = ("RANGE_FADE", "BREAKOUT", "PULLBACK")
_SETUP_RE = re.compile(r"\b(" + "|".join(SETUPS) + r")\b")


def setup_of(*texts: str | None) -> str:
    for t in texts:
        m = _SETUP_RE.search(t or "")
        if m:
            return m.group(1)
    return "UNNAMED"


def collect(journal, broker, days: int = 7, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    rows = journal.db.execute(
        "SELECT o.cycle_id, o.ticket, o.order_type, o.agent_reason, c.summary FROM orders o "
        "LEFT JOIN cycles c ON c.cycle_id = o.cycle_id "
        "WHERE o.sent = 1 AND o.ok = 1 AND o.ticket IS NOT NULL AND o.ts >= ? "
        "AND o.cycle_id NOT LIKE 'manual%' AND o.cycle_id NOT LIKE 'swing-%' ORDER BY o.ts",
        (since,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = out.setdefault(setup_of(r["summary"], r["agent_reason"]),
                           {"placed": 0, "filled": 0, "wins": 0, "losses": 0, "net": 0.0,
                            "exits": {}, "open": 0})
        s["placed"] += 1
        try:
            d = broker.deal_history(int(r["ticket"]))
        except Exception:  # broker hiccup: skip, stats are advisory only
            d = None
        if not d:
            continue  # pending never filled (or no history yet)
        s["filled"] += 1
        if not d.get("closed"):
            s["open"] += 1
            continue
        net = float(d.get("net_profit") or 0.0)
        s["net"] = round(s["net"] + net, 2)
        s["wins" if net > 0 else "losses"] += 1
        reason = d.get("exit_reason") or "?"
        s["exits"][reason] = s["exits"].get(reason, 0) + 1
    return out


def stats_text(journal, broker, days: int = 7) -> str:
    try:
        data = collect(journal, broker, days)
    except Exception as exc:  # never block a cycle on stats
        return f"(setup stats unavailable: {exc})"
    if not data:
        return "(no realized trades yet)"
    lines = []
    for name in (*SETUPS, "UNNAMED"):
        s = data.get(name)
        if not s:
            continue
        exits = ", ".join(f"{k}:{v}" for k, v in sorted(s["exits"].items())) or "-"
        lines.append(
            f"- {name}: placed {s['placed']}, filled {s['filled']}, closed {s['wins']}W/{s['losses']}L, "
            f"net {s['net']:+.2f} USD, exits {exits}" + (f", open {s['open']}" if s["open"] else "")
        )
    lines.append("Small samples are weak evidence: use them to be more selective, not to abandon the playbook.")
    return "\n".join(lines)
