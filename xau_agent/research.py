"""Event study on broker history: where does price drift enough to beat costs?

Uses ONLY the in-sample part (first 70% of M5 history) so the out-of-sample part stays untouched for the final
check of any strategy built from these findings.

    python -m xau_agent.research
Forward moves are measured on bid closes, in points and in H1-ATR units, at +60 and +240 minutes.
A condition is interesting only if its mean forward move clearly exceeds the round-trip cost (~70 points).
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from statistics import mean

from .backtest import POINT, Agg, Ind
from .history import DATA_DIR, load

COST_POINTS = 70


def features(m5: list[dict], end: int) -> list[dict]:
    m5ind = Ind()
    tfs = {"M15": Agg(900), "M30": Agg(1800), "H1": Agg(3600)}
    h1_hist: deque = deque(maxlen=21)  # completed H1 closes/highs/lows for Donchian
    last_h1_n = 0
    out = []
    for i, b in enumerate(m5):
        if b["time"] >= end:
            break
        m5ind.push(b["high"], b["low"], b["close"])
        for a in tfs.values():
            a.add(b)
        h1 = tfs["H1"].ind
        if h1.n != last_h1_n:
            last_h1_n = h1.n
            h1_hist.append((h1.closes[-1],))
        if not (m5ind.ready() and all(a.ind.ready() for a in tfs.values())) or i + 48 >= len(m5):
            continue
        trends = {k: a.ind.trend() for k, a in tfs.items()}
        ts = set(trends.values())
        bias = ts.pop() if len(ts) == 1 and ts <= {"up", "down"} else "mixed"
        c = b["close"]
        ext = (c - h1.ema20) / h1.atr
        closes20 = [x[0] for x in list(h1_hist)[:-1]]
        don = None
        if len(closes20) >= 20:
            don = "up" if h1.closes[-1] > max(closes20) else "down" if h1.closes[-1] < min(closes20) else "in"
        f60 = (m5[i + 12]["close"] - c) / POINT
        f240 = (m5[i + 48]["close"] - c) / POINT
        out.append({
            "t": b["time"] + 300,
            "hour": datetime.fromtimestamp(b["time"] + 300, tz=timezone.utc).hour,
            "bias": bias,
            "ext": ext,
            "don": don,
            "atr_h1_pts": h1.atr / POINT,
            "f60": f60,
            "f240": f240,
        })
    return out


def table(rows: list[dict], key, title: str, direction=None) -> None:
    groups = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            groups[k].append(r)
    print(f"\n## {title}")
    print(f"{'group':>14} {'n':>7} {'mean f60':>9} {'mean f240':>10} {'up%240':>7} {'f240/ATRh1':>10}")
    for k in sorted(groups, key=str):
        g = groups[k]
        sign = direction(k) if direction else 1
        f60 = mean(x["f60"] for x in g) * sign
        f240 = mean(x["f240"] for x in g) * sign
        up = mean(1 if x["f240"] * sign > 0 else 0 for x in g)
        norm = mean(x["f240"] / x["atr_h1_pts"] for x in g) * sign
        flag = "  <-- > cost" if abs(f240) > COST_POINTS and len(g) >= 300 else ""
        print(f"{str(k):>14} {len(g):7d} {f60:9.1f} {f240:10.1f} {up:7.1%} {norm:10.3f}{flag}")


def main() -> None:
    m5 = load(DATA_DIR / "GOLD_M5.csv")
    split = m5[int(len(m5) * 0.7)]["time"]
    rows = features(m5, split)
    print(f"in-sample decisions: {len(rows)} (until {datetime.fromtimestamp(split, tz=timezone.utc).date()}), "
          f"cost ≈ {COST_POINTS} pts round trip")
    base = mean(r["f240"] for r in rows)
    print(f"unconditional mean f240: {base:.1f} pts (gold drifted up in this period; compare groups to this)")
    table(rows, lambda r: r["bias"], "Higher-timeframe bias (M15+M30+H1): move in bias direction",
          direction=lambda k: 1 if k == "up" else -1 if k == "down" else 1)
    table(rows, lambda r: r["hour"], "Server hour (raw move, buy side)")
    table(rows, lambda r: r["don"], "H1 close vs prior 20 H1 closes (Donchian): move in breakout direction",
          direction=lambda k: 1 if k == "up" else -1 if k == "down" else 1)
    table(rows, lambda r: (">+2" if r["ext"] > 2 else "<-2" if r["ext"] < -2 else ">+1" if r["ext"] > 1
                           else "<-1" if r["ext"] < -1 else "mid"),
          "Extension from H1 EMA20 in H1 ATR: move toward EMA (mean reversion)",
          direction=lambda k: -1 if k.startswith(">") else 1 if k.startswith("<") else 1)


if __name__ == "__main__":
    main()
