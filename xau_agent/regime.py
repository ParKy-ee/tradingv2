"""Range/chop filter study for the trend-following M5 setups (BREAKOUT, PULLBACK).

Problem seen live (2026-11-25): M15/M30/H1 EMAs still said "down" while price had stopped falling and was
chopping in a 3535-3560 box, so the agent sold the bottom of the box again and again (5 SL in a row).

Filters, declared BEFORE running (parameters fixed, no sweep):
  F1 PROGRESS : trade with the trend only if the trend made a new extreme recently: for a sell, the lowest low of
                the last 24 M5 bars (2 h) is below the lowest low of the 24 bars before it (buy: mirror).
  F2 EFFICIENCY: Kaufman efficiency ratio over the last 24 M5 closes >= 0.30
                (|net move| / sum of |bar-to-bar moves|; ~0 = chop, 1 = straight line).
  F1+F2       : both.

Decision rule, also declared before running: adopt a filter as a hard risk rule only if, for BOTH setups,
  (a) avg R of the kept trades beats the unfiltered avg R in-sample AND out-of-sample, and
  (b) the trades it removes have a lower avg R than the ones it keeps (it removes the bad ones, not random ones),
with at least 100 kept trades per setup in the full sample. Even if adopted, a filter that leaves the setup
negative after costs does not make the M5 setups tradeable; it only cuts losses in dev mode.

    python -m xau_agent.regime
"""
from __future__ import annotations

import json
from pathlib import Path

from .backtest import SETUPS, Config, State, metrics, run
from .history import DATA_DIR, load

N = 24


def progress_ok(s: State, side: str) -> bool:
    h = s.hist
    if len(h) < 2 * N:
        return False
    last, prev = h[-N:], h[-2 * N:-N]
    if side == "sell":
        return min(x["low"] for x in last) < min(x["low"] for x in prev)
    return max(x["high"] for x in last) > max(x["high"] for x in prev)


def efficiency(s: State) -> float | None:
    h = s.hist
    if len(h) < N + 1:
        return None
    closes = [x["close"] for x in h[-(N + 1):]]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / path if path else 0.0


def eff_ok(s: State, side: str) -> bool:
    e = efficiency(s)
    return e is not None and e >= 0.30


FILTERS = {
    "F1": lambda s, side: progress_ok(s, side),
    "F2": lambda s, side: eff_ok(s, side),
    "F1+F2": lambda s, side: progress_ok(s, side) and eff_ok(s, side),
}


def wrap(base: str, flt: str, keep: bool):
    fn, f = SETUPS[base], FILTERS[flt]

    def setup(s: State):
        o = fn(s)
        if o and f(s, o.side) == keep:
            return o
        return None
    return setup


def main() -> None:
    m5 = load(DATA_DIR / "GOLD_M5.csv")
    split = m5[int(len(m5) * 0.7)]["time"]
    periods = {"full": Config(), "in_sample": Config(end=split), "out_of_sample": Config(start=split),
               "full_no_costs": Config(spread_points=0, slippage_points=0)}
    out: dict = {}
    lines = []
    for base in ("BREAKOUT", "PULLBACK"):
        variants = {"none": None, **{f"{k}_keep": (k, True) for k in FILTERS},
                    **{f"{k}_removed": (k, False) for k in FILTERS}}
        out[base] = {}
        for vname, spec in variants.items():
            name = f"{base}|{vname}"
            SETUPS[name] = SETUPS[base] if spec is None else wrap(base, spec[0], spec[1])
            res = {}
            for p, cfg in periods.items():
                r = run([name], cfg, m5)
                res[p] = metrics(r["trades"])
            out[base][vname] = res
            g = lambda p, k: res[p].get(k, 0) or 0  # noqa: E731
            lines.append(f"{base:9s} {vname:16s} n {g('full','trades'):4d}  win {g('full','win_rate'):.0%}  "
                         f"avgR full {g('full','avg_r'):+.3f}  IS {g('in_sample','avg_r'):+.3f}  "
                         f"OOS {g('out_of_sample','avg_r'):+.3f}  no-cost {g('full_no_costs','avg_r'):+.3f}  "
                         f"net$ {g('full','net_usd_0.01lot'):+.2f}")
    path = Path(__file__).resolve().parent.parent / "exports" / "regime.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("\n".join(lines))
    print(f"-> {path}")


if __name__ == "__main__":
    main()
