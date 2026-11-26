"""Exit-protection study for the H4 Donchian 20/10 swing rule: would locking profit early help or hurt?

Trigger (2026-11-26): both first forward trades went into profit (GOLD +0.53R, BTC +0.28R) and then reversed to the
full stop. Question: does a protective exit improve the rule over many years, or does it cut the big winners the
rule lives on? Declared BEFORE running; canonical values, no sweep. Entries are identical in every variant.

  BASE      the rule as traded: 2x ATR20 broker stop, exit on a close beyond the 10-bar channel, no take-profit
  BE_1R     once open profit reaches +1R, move the stop to entry (+ costs)
  BE_05R    same at +0.5R (would have saved today's GOLD trade)
  TRAIL_3A  add a chandelier trailing stop 3x ATR20 from the best price since entry
  TP_2R     add a fixed take-profit at +2R
  PART_1R   close half at +1R, the rest runs under the rule
  TIME_6    close at the next open if after 6 bars (24 h) the trade never reached +0.5R

Fill model: as xau_agent/swing.py (stop fills at the stop or the open if gapped through, costs scaled to price).
Bars are H4, so the order of events inside a bar is unknown: a level reached inside bar k only activates from bar
k+1 (break-even / trailing), and if a bar touches both stop and target, the stop counts (conservative).
PASS for a variant: avg R above BASE on GOLD and on BTCUSD, in both halves of each sample, and max drawdown in R
no worse than BASE + 10%. Otherwise BASE stays.

    python -m xau_agent.exits
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timezone

from .history import DATA_DIR, load
from .swing import atr

VARIANTS = ["BASE", "BE_1R", "BE_05R", "TRAIL_3A", "TP_2R", "PART_1R", "TIME_6"]


def run(bars: list[dict], variant: str, costs: tuple[float, float, float, float], t_start: int) -> list[dict]:
    spread_f, slip_f, swl_f, sws_f = costs
    trades, pos = [], None
    nights = lambda a, b: max(0, int((b - a) // 86400))  # noqa: E731  (calendar nights; swap is small at H4 holds)

    def close(i, px, frac=1.0, why=""):
        nonlocal pos
        side, e = pos["side"], pos["entry"]
        pnl = (px - e) if side == "buy" else (e - px)
        pnl += nights(pos["t"], bars[i]["time"]) * (swl_f if side == "buy" else sws_f) * e
        pos["realized"] += frac * pnl
        pos["left"] -= frac
        if pos["left"] <= 1e-9:
            trades.append({"t0": pos["t"], "r": pos["realized"] / pos["risk"], "why": why, "side": side})
            pos = None

    for i in range(22, len(bars) - 1):
        b, nxt = bars[i], bars[i + 1]
        if b["time"] < t_start:
            continue
        o_spr = spread_f * b["open"]
        if pos:
            side, e, R = pos["side"], pos["entry"], pos["risk"]
            # protective levels active for this bar (set from previous bars)
            stop = pos["stop"]
            if side == "buy" and b["low"] <= stop:
                close(i, min(stop, b["open"]) - slip_f * b["open"], 1.0, "stop")
            elif side == "sell" and b["high"] + o_spr >= stop:
                close(i, max(stop, b["open"] + o_spr) + slip_f * b["open"], 1.0, "stop")
            if pos and pos.get("tp") is not None:
                tp = pos["tp"]
                if (side == "buy" and b["high"] >= tp) or (side == "sell" and b["low"] + o_spr <= tp):
                    close(i, tp, pos["left"] if variant == "TP_2R" else 0.5, "tp")
                    if pos:
                        pos["tp"] = None
            if pos:
                fav = (b["high"] - e) if side == "buy" else (e - (b["low"] + o_spr))
                pos["mfe"] = max(pos["mfe"], fav / R)
                pos["best"] = max(pos["best"], b["high"]) if side == "buy" else min(pos["best"], b["low"])
                pos["bars"] += 1
                # update protective stop for the NEXT bar
                cost = (spread_f + 2 * slip_f) * e
                if variant in ("BE_1R", "BE_05R") and pos["mfe"] >= (1.0 if variant == "BE_1R" else 0.5):
                    be = e + cost if side == "buy" else e - cost
                    pos["stop"] = max(pos["stop"], be) if side == "buy" else min(pos["stop"], be)
                if variant == "TRAIL_3A":
                    a = atr(bars, i, 20)
                    tr = pos["best"] - 3 * a if side == "buy" else pos["best"] + 3 * a
                    pos["stop"] = max(pos["stop"], tr) if side == "buy" else min(pos["stop"], tr)
        # channel signals at the close of bar i, executed at the next open
        hi = max(x["high"] for x in bars[i - 20:i])
        lo = min(x["low"] for x in bars[i - 20:i])
        want = "buy" if b["close"] > hi else "sell" if b["close"] < lo else None
        o = nxt["open"]
        if pos:
            xlo = min(x["low"] for x in bars[i - 10:i])
            xhi = max(x["high"] for x in bars[i - 10:i])
            ex = (pos["side"] == "buy" and b["close"] < xlo) or (pos["side"] == "sell" and b["close"] > xhi)
            if variant == "TIME_6" and pos["bars"] >= 6 and pos["mfe"] < 0.5:
                ex = True
            if ex:
                px = o - slip_f * o if pos["side"] == "buy" else o + (spread_f + slip_f) * o
                close(i + 1, px, pos["left"], "exit")
        if not pos and want:
            a = atr(bars, i, 20)
            fill = o + (spread_f + slip_f) * o if want == "buy" else o - slip_f * o
            R = 2 * a
            pos = {"side": want, "entry": fill, "risk": R, "t": nxt["time"], "realized": 0.0, "left": 1.0,
                   "stop": fill - R if want == "buy" else fill + R, "mfe": 0.0, "best": fill, "bars": 0, "tp": None}
            if variant == "TP_2R":
                pos["tp"] = fill + 2 * R if want == "buy" else fill - 2 * R
            if variant == "PART_1R":
                pos["tp"] = fill + R if want == "buy" else fill - R
    return trades


def stats(tr: list[dict]) -> dict:
    if len(tr) < 3:
        return {"n": len(tr)}
    r = [t["r"] for t in tr]
    eq = peak = dd = 0.0
    for x in r:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    mu, sd = st.mean(r), st.stdev(r)
    return {"n": len(r), "avg": mu, "t": mu / sd * len(r) ** 0.5, "win": sum(x > 0 for x in r) / len(r), "dd": dd,
            "sum": sum(r), "big": sum(x >= 3 for x in r)}


def main() -> None:
    from . import multi
    import MetaTrader5 as mt5
    mt5.initialize()
    results = {}
    for sym in ("GOLD", "BTCUSD"):
        bars = load(DATA_DIR / f"{sym}_H4.csv")
        i = mt5.symbol_info(sym)
        meta = {"point": i.point, "spread_now": i.spread, "swap_mode": i.swap_mode, "swap_long": i.swap_long,
                "swap_short": i.swap_short, "contract": i.trade_contract_size}
        costs = multi.costs_for(bars, meta)[0] if sym != "GOLD" else None
        if costs is None:
            from .swing import DEFAULT_COSTS
            costs = DEFAULT_COSTS
        t0 = int(datetime(2013, 6, 10, tzinfo=timezone.utc).timestamp()) if sym == "GOLD" else multi.real_h4_start(bars)
        mid = t0 + (bars[-1]["time"] - t0) // 2
        print(f"\n{sym}  variant    n   win   avgR   1st half  2nd half   t     maxDD(R)  trades>=3R  sumR")
        for v in VARIANTS:
            tr = run(bars, v, costs, t0)
            s, h1, h2 = stats(tr), stats([t for t in tr if t["t0"] < mid]), stats([t for t in tr if t["t0"] >= mid])
            results[(sym, v)] = (s, h1, h2)
            print(f"{'':6s}{v:9s} {s['n']:4d} {s['win']:.0%} {s['avg']:+.3f}   {h1['avg']:+.3f}    {h2['avg']:+.3f}  "
                  f"{s['t']:+5.2f}   {s['dd']:6.1f}     {s['big']:4d}    {s['sum']:+.0f}")
    print()
    for v in VARIANTS[1:]:
        ok = True
        for sym in ("GOLD", "BTCUSD"):
            s, h1, h2 = results[(sym, v)]
            b, b1, b2 = results[(sym, "BASE")]
            ok &= s["avg"] > b["avg"] and h1["avg"] > b1["avg"] and h2["avg"] > b2["avg"] and s["dd"] <= b["dd"] * 1.1
        print(f"{v:9s} {'PASS' if ok else '-'}")


if __name__ == "__main__":
    main()
