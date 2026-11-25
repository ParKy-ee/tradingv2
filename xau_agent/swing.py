"""Swing trend-following tests on D1/H4 gold history (2001-2026), canonical parameters only (no tuning).

    python -m xau_agent.swing
Rules (fixed in advance, from the trend-following literature; nothing fitted to this data):
- DON20_10:  Donchian breakout. Enter next open after close > prior 20-bar high (short: < 20-bar low).
             Initial stop 2x ATR20. Exit next open after close < prior 10-bar low (short: > 10-bar high).
- DON55_20:  same with 55 / 20 bars.
- SMA50_200: long while SMA50 > SMA200, short while below; switch at next open on a cross; 3x ATR20 stop.
Costs scale with price (today's XM GOLD terms applied to all years): spread 55 pts and slippage 10 pts at 3620,
swap long -69.5 / short +15.07 pts per night at 3620, triple on Wednesday. Stops fill at the stop or worse (gap).
R = profit / initial risk (2x ATR20 for Donchian, 3x ATR20 for SMA).
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean
from typing import Any

from .history import DATA_DIR, load

REF_PRICE = 3620.0
SPREAD_FRAC = 0.55 / REF_PRICE
SLIP_FRAC = 0.10 / REF_PRICE
SWAP_LONG_FRAC = -0.695 / REF_PRICE  # per night, price units / price
SWAP_SHORT_FRAC = 0.1507 / REF_PRICE
DEFAULT_COSTS = (SPREAD_FRAC, SLIP_FRAC, SWAP_LONG_FRAC, SWAP_SHORT_FRAC)


def atr(bars, i, n=20):
    trs = [max(bars[k]["high"] - bars[k]["low"], abs(bars[k]["high"] - bars[k - 1]["close"]),
               abs(bars[k]["low"] - bars[k - 1]["close"])) for k in range(i - n + 1, i + 1)]
    return sum(trs) / n


def _nights(t0: int, t1: int) -> int:
    """Rollover nights between two times, Wednesday counted 3x (weekend)."""
    d0 = datetime.fromtimestamp(t0, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(t1, tz=timezone.utc).date()
    n, d = 0, d0
    from datetime import timedelta
    while d < d1:
        wd = d.weekday()
        if wd < 5:
            n += 3 if wd == 2 else 1
        d += timedelta(days=1)
    return n


def backtest(bars: list[dict], rule: str, sides=("buy", "sell"), t_start: int = 0,
             costs: tuple[float, float, float, float] | None = None) -> list[dict]:
    """rule: DON20_10 | DON55_20 | SMA50_200 | BASE20_10 | BASE55_20 (BASE = enter whenever flat, same exits:
    the no-signal baseline that shows how much of a result is just drift)."""
    """costs = (spread, slippage, swap_long, swap_short) as fractions of price; default = XM GOLD."""
    SPREAD_FRAC, SLIP_FRAC, SWAP_LONG_FRAC, SWAP_SHORT_FRAC = costs or DEFAULT_COSTS
    trades: list[dict] = []
    pos = None
    blocked = None  # SMA: side stopped out; re-enter only after the trend flips and comes back
    closes = [b["close"] for b in bars]
    entry_n, exit_n = {"DON20_10": (20, 10), "DON55_20": (55, 20), "BASE20_10": (20, 10),
                       "BASE55_20": (55, 20)}.get(rule, (0, 0))
    sma = {}
    if rule == "SMA50_200":
        for p in (50, 200):
            s, run = [None] * len(bars), 0.0
            for i, c in enumerate(closes):
                run += c
                if i >= p:
                    run -= closes[i - p]
                if i >= p - 1:
                    s[i] = run / p
            sma[p] = s
    start = 201 if rule == "SMA50_200" else entry_n + 1

    def close_pos(i_bar, price, reason):
        nonlocal pos, blocked
        b = bars[i_bar]
        side, entry = pos["side"], pos["entry"]
        pnl = (price - entry) if side == "buy" else (entry - price)
        frac = SWAP_LONG_FRAC if side == "buy" else SWAP_SHORT_FRAC
        swap = _nights(pos["t"], b["time"]) * frac * entry
        pnl += swap
        if reason == "stop":
            blocked = side
        trades.append({"side": side, "t0": pos["t"], "t1": b["time"], "entry": entry, "exit": price,
                       "pnl": pnl, "swap": swap, "r": pnl / pos["risk"], "pct": pnl / entry, "reason": reason})
        pos = None

    for i in range(start, len(bars) - 1):
        b, nxt = bars[i], bars[i + 1]
        # stop check on this bar (intrabar), fill at stop or the open if gapped through
        if b["time"] < t_start:
            continue
        if pos:
            if pos["side"] == "buy" and b["low"] <= pos["stop"]:
                close_pos(i, min(pos["stop"], b["open"]) - SLIP_FRAC * b["open"], "stop")
            elif pos["side"] == "sell" and b["high"] + SPREAD_FRAC * b["open"] >= pos["stop"]:
                close_pos(i, max(pos["stop"], b["open"]) + (SPREAD_FRAC + SLIP_FRAC) * b["open"], "stop")
        # signals at the close of bar i, executed at the open of bar i+1
        want = None
        exit_now = False
        if rule.startswith("DON") or rule.startswith("BASE"):
            hi = max(x["high"] for x in bars[i - entry_n:i])
            lo = min(x["low"] for x in bars[i - entry_n:i])
            if b["close"] > hi:
                want = "buy"
            elif b["close"] < lo:
                want = "sell"
            if rule.startswith("BASE"):
                want = sides[0]  # always want to be in the market on the tested side
            if pos:
                xlo = min(x["low"] for x in bars[i - exit_n:i])
                xhi = max(x["high"] for x in bars[i - exit_n:i])
                exit_now = (pos["side"] == "buy" and b["close"] < xlo) or (pos["side"] == "sell" and b["close"] > xhi)
        else:
            want = "buy" if sma[50][i] > sma[200][i] else "sell"
            if blocked and want != blocked:
                blocked = None
            exit_now = bool(pos) and pos["side"] != want
        o = nxt["open"]
        if pos and exit_now:
            px = o - SLIP_FRAC * o if pos["side"] == "buy" else o + (SPREAD_FRAC + SLIP_FRAC) * o
            close_pos(i + 1, px, "exit")
        if not pos and want in sides and (rule == "SMA50_200" or want):
            a = atr(bars, i)
            k = 3.0 if rule == "SMA50_200" else 2.0
            fill = o + (SPREAD_FRAC + SLIP_FRAC) * o if want == "buy" else o - SLIP_FRAC * o
            stop = fill - k * a if want == "buy" else fill + k * a
            if rule == "SMA50_200":
                if blocked == want:
                    continue  # stopped out on this side: wait for the next cross
                blocked = None
            pos = {"side": want, "entry": fill, "stop": stop, "risk": k * a, "t": nxt["time"]}
    if pos:
        close_pos(len(bars) - 1, bars[-1]["close"], "open_at_end")
    return trades


def stats(tr: list[dict]) -> dict[str, Any]:
    if not tr:
        return {"n": 0}
    wins = [t for t in tr if t["pnl"] > 0]
    gl = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    return {"n": len(tr), "win": len(wins) / len(tr), "avg_r": mean(t["r"] for t in tr),
            "pf": (sum(t["pnl"] for t in wins) / gl) if gl > 0 else None,
            "sum_pct": sum(t["pct"] for t in tr) * 100,
            "swap_share": sum(t["swap"] for t in tr) / max(1e-9, sum(abs(t["pnl"]) for t in tr))}


def bh_pct(bars, t0, t1) -> float:
    seg = [b for b in bars if t0 <= b["time"] < t1]
    return (seg[-1]["close"] / seg[0]["close"] - 1) * 100 if len(seg) > 1 else 0.0


def main() -> None:
    ts = lambda y: int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())  # noqa: E731
    for tf in ("D1", "H4"):
        bars = load(DATA_DIR / f"GOLD_{tf}.csv")
        t_start = ts(2013) + 160 * 86400 if tf == "H4" else 0  # XM H4 history is real 4h only from 2013-05
        periods = ([("2013-2018", 2013, 2019), ("2019-2026", 2019, 2027)] if tf == "H4" else
                   [("2001-2010", 2001, 2011), ("2011-2018", 2011, 2019), ("2019-2026", 2019, 2027)])
        print(f"\n=== {tf}: {len(bars)} bars, {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc).date()} .. "
              f"{datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc).date()}")
        print("  buy&hold %: " + "  ".join(f"{n} {bh_pct(bars, ts(a), ts(b)):+.0f}%" for n, a, b in periods))
        for rule in ("DON20_10", "BASE20_10", "DON55_20", "BASE55_20", "SMA50_200"):
            for side in ("buy", "sell"):
                tr = backtest(bars, rule, sides=(side,), t_start=t_start)
                s = stats(tr)
                if not s["n"]:
                    print(f"  {rule:9s} {side:4s} n=0")
                    continue
                per = []
                for n, a, b in periods:
                    p = stats([t for t in tr if ts(a) <= t["t0"] < ts(b)])
                    per.append(f"{n}: n={p['n']} R={p.get('avg_r', 0):+.2f}")
                pf = f"{s['pf']:.2f}" if s["pf"] else "-"
                print(f"  {rule:9s} {side:4s} n={s['n']:4d} win={s['win']:.0%} avgR={s['avg_r']:+.3f} PF={pf} "
                      f"sum%={s['sum_pct']:+.0f} | " + " | ".join(per))


if __name__ == "__main__":
    main()
