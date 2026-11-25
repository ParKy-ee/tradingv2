"""H1 trend-following candidates vs drift baselines (research; not used by the live agent yet).

Every candidate is compared with a baseline that enters in the same direction at EVERY H1 close with the same
exits. Beating the baseline means edge from the signal, not just from gold drifting up in the sample.

    python -m xau_agent.h1lab            # in-sample only
    python -m xau_agent.h1lab --oos      # out-of-sample check (run once, for the shortlisted candidates)
"""
from __future__ import annotations

import sys
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from .backtest import Agg, Config, Engine, Ind, Order, metrics
from .history import DATA_DIR, load


def run_h1(decide: Callable[[dict], tuple[str, ...] | None], m5: list[dict], start: int, end: int,
           k_sl: float, k_tp: float, hold_h: int, sides=("buy", "sell"), cfg: Config | None = None):
    cfg = cfg or Config()
    eng = Engine([], cfg)
    m5ind = Ind()
    tfs = {"M15": Agg(900), "M30": Agg(1800), "H1": Agg(3600)}
    h1c: deque = deque(maxlen=60)
    last_n, prev_bias = 0, None
    for i, b in enumerate(m5):
        m5ind.push(b["high"], b["low"], b["close"])
        for a in tfs.values():
            a.add(b)
        h1 = tfs["H1"].ind
        if h1.n != last_n:
            last_n = h1.n
            ready = all(a.ind.ready() for a in tfs.values())
            trends = {k: a.ind.trend() for k, a in tfs.items()}
            ts = set(trends.values())
            bias = ts.pop() if len(ts) == 1 and ts <= {"up", "down"} else None
            ctx = {"close": h1.closes[-1], "prev": list(h1c), "bias": bias, "prev_bias": prev_bias,
                   "h1": h1, "trends": trends}
            h1c.append(h1.closes[-1])
            prev_bias = bias
            if ready and start <= b["time"] < end and not eng.pos and not eng.order:
                side = decide(ctx)
                if side in sides:
                    t = b["time"] + 300
                    c, a = h1.closes[-1], h1.atr
                    sl = c - k_sl * a if side == "buy" else c + k_sl * a
                    tp = c + k_tp * a if side == "buy" else c - k_tp * a
                    eng.order = Order(decide.__name__, side, "market", c, sl, tp, t + 600, t, hold_h * 60)
                    eng.placed += 1
        if i + 1 < len(m5):
            eng._on_bar(m5[i + 1])
    return eng.trades


def donchian20(ctx):
    p = ctx["prev"][-20:]
    if len(p) < 20:
        return None
    return "buy" if ctx["close"] > max(p) else "sell" if ctx["close"] < min(p) else None


def bias_flip(ctx):
    if ctx["bias"] and ctx["bias"] != ctx["prev_bias"]:
        return "buy" if ctx["bias"] == "up" else "sell"
    return None


def baseline_every_hour(ctx):  # side chosen by `sides`
    return "both"


def _row(name, side, m):
    if not m.get("trades"):
        return f"{name:22s} {side:5s} n=0"
    return (f"{name:22s} {side:5s} n={m['trades']:4d} win={m['win_rate']:.0%} avgR={m['avg_r']:+.3f} "
            f"PF={m['profit_factor']} net=${m['net_usd_0.01lot']:+.0f} exits={m['exits']}")


def main() -> None:
    oos = "--oos" in sys.argv
    m5 = load(DATA_DIR / "GOLD_M5.csv")
    split = m5[int(len(m5) * 0.7)]["time"]
    start, end = (split, 2 ** 40) if oos else (0, split)
    label = "OUT-OF-SAMPLE" if oos else "IN-SAMPLE"
    d = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).date()  # noqa: E731
    print(f"{label}: {d(max(start, m5[0]['time']))} .. {d(min(end, m5[-1]['time']))} | price "
          f"{next(b for b in m5 if b['time'] >= start)['close']:.0f} -> "
          f"{[b for b in m5 if b['time'] < end][-1]['close']:.0f}")
    for k_sl, k_tp, hold in ((1.0, 2.0, 24), (1.5, 3.0, 48)):
        print(f"\n### exits: SL {k_sl}x H1 ATR, TP {k_tp}x H1 ATR, max hold {hold}h")
        res: dict[str, Any] = {}
        for fn in (donchian20, bias_flip):
            for side in ("buy", "sell"):
                m = metrics(run_h1(fn, m5, start, end, k_sl, k_tp, hold, sides=(side,)))
                res[(fn.__name__, side)] = m
                print(_row(fn.__name__, side, m))
        for side in ("buy", "sell"):
            fn = (lambda ctx, s=side: s)
            fn.__name__ = "baseline_every_hour"
            print(_row("baseline_every_hour", side, metrics(run_h1(fn, m5, start, end, k_sl, k_tp, hold,
                                                                     sides=(side,)))))


if __name__ == "__main__":
    main()
