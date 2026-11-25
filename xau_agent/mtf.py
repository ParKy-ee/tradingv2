"""Multi-timeframe alignment: one deterministic "analyst" per timeframe, combined into a bias.

Higher-timeframe bias (M15, M30, H1 all pointing the same way) is shown to the agent and enforced by the
risk engine (BLOCK_AGAINST_HTF): no new entries against it.
"""
from __future__ import annotations

from typing import Any

from . import indicators

MTF_TFS = ("M1", "M5", "M15", "M30", "H1")
HTF = ("M15", "M30", "H1")
_ARROW = {"up": "up", "down": "down", "flat": "flat", None: "n/a"}


def summarize(trends: dict[str, str | None]) -> dict[str, Any]:
    ups = [tf for tf in MTF_TFS if trends.get(tf) == "up"]
    downs = [tf for tf in MTF_TFS if trends.get(tf) == "down"]
    htf = {trends.get(tf) for tf in HTF}
    bias = htf.pop() if len(htf) == 1 and htf <= {"up", "down"} else None
    line = " | ".join(f"{tf} {_ARROW[trends.get(tf)]}" for tf in MTF_TFS)
    return {
        "line": line,
        "up_count": len(ups),
        "down_count": len(downs),
        "htf_bias": bias,
        "note": (f"M15, M30 and H1 all {bias}: new entries against this are blocked by the risk engine"
                 if bias else "higher timeframes not aligned: no directional block"),
    }


def frame_trend(broker, tf: str) -> str | None:
    rows = broker.bars(tf, 200)
    t = indicators.trend([r["high"] for r in rows], [r["low"] for r in rows], [r["close"] for r in rows])
    return t["trend"] if t else None


def htf_bias(broker) -> str | None:
    return summarize({tf: frame_trend(broker, tf) for tf in HTF})["htf_bias"]
