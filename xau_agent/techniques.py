"""Intraday technique suite on GOLD M5: which well-known techniques survive real costs? (many trades per day)

Declared BEFORE running (2026-11-25 18:30 Thai). Canonical textbook parameters only, no tuning, no sweep.
Data: data/GOLD_M5.csv (XM, ~100k bars, 2025-06..2026-11). Server time = New York time + 7 h on XM all year,
so sessions are defined in server time: Asia 01:00-10:00, London open 10:00, NY cash open 16:30 (09:30 NY).

Execution model (same spirit as backtest.py): signal at an M5 close, market entry at the next bar's open;
buy pays the ask (= bid + bar spread, min 55 pts) + 10 pts slippage, sell gets bid − 10 pts; stop exits pay
slippage, take-profit exits do not; if stop and target are inside the same bar the stop counts; one position
per technique at a time; no trades Fri 22:00 – Mon 01:00 server. R = P&L / initial stop distance.

Techniques (exits fixed per family):
  Mean reversion — stop 1.5x ATR14, target 1.0x ATR14, time exit after 12 bars (1 h):
    RSI2      Connors: RSI(2) < 5 buy, > 95 sell
    RSI14     RSI(14) crosses back above 30 buy / below 70 sell
    BB20      close outside Bollinger(20, 2) then next close back inside -> fade
    STOCH     Stochastic(14,3) %K crosses %D below 20 buy / above 80 sell
  Trend / breakout — stop 1.5x ATR14, target 3.0x ATR14, time exit after 48 bars (4 h):
    EMA9_21   EMA9 crosses EMA21
    MACD      MACD(12,26,9) crosses its signal line
    DON20     close beyond the prior 20-bar high/low
    ORB_LON   London: range of 10:00-10:30 server; first close beyond it until 14:00 (1 trade/day max)
    ORB_NY    New York: range of 16:30-17:00 server; first close beyond it until 20:00 (1 trade/day max)
    ASIA_BRK  Asian range 01:00-10:00; first close beyond it 10:00-14:00 (1 trade/day max)
  Time-of-day (academic "gold rises in Asia, falls later" pattern) — stop 3.5x ATR14 (≈ H1 ATR), exit by time:
    ASIA_LONG buy at the first bar close of the day (01:05; XM opens 01:00), exit 10:00
    DAY_SHORT sell 10:00, exit 22:00
  Intraday momentum (Gao et al. 2018, first half hour predicts the last):
    NY_MOM    at 17:00 trade the sign of the 16:30-17:00 move, exit 22:30; stop 3.5x ATR14
  News reaction (declared 2026-11-25 17:25 Thai, before running; added after the suite above had failed):
    Scheduled US releases happen at 08:30, 10:00 and 14:00 New York time = 15:30, 17:00, 21:00 server.
    Event = the 15 minutes after the release move more than 1.5x ATR14 (ATR measured on the bar before the
    release). Trade WITH that first move at the 15-minute close ("does the news move continue?"), stop 3.5x ATR14,
    exit by time. No actual-vs-forecast data is needed; the market's first reaction is the signal.
    NEWS0830_2H / NEWS0830_4H   decide 15:45, exit 17:45 / 19:45
    NEWS1000_2H                 decide 17:15, exit 19:15
    NEWS1400_2H                 decide 21:15, exit 23:15
    PASS for NEWS tests: n >= 60 (events are rarer); the other conditions as below.

PASS (all required): n >= 200; avg R after costs > 0 in-sample (first 70% of time) AND out-of-sample (last 30%);
t-stat of R >= 2 on the full sample. ~14 tests: at t >= 2 roughly one false pass is possible by luck.
Passing techniques are combined into a portfolio to see total trades/day and combined R.

    python -m xau_agent.techniques
"""
from __future__ import annotations

import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .history import DATA_DIR, load

POINT = 0.01
SLIP = 10 * POINT


# ---------------------------------------------------------------- indicators (full arrays, value at index i uses bars <= i)
def ema(x: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    k = 2 / (n + 1)
    if len(x) < n:
        return out
    v = sum(x[:n]) / n
    out[n - 1] = v
    for i in range(n, len(x)):
        v = x[i] * k + v * (1 - k)
        out[i] = v
    return out


def rsi(c: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g, l = g / n, l / n
    out[n] = 100 - 100 / (1 + g / l) if l else 100.0
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + g / l) if l else 100.0
    return out


def atr(b: list[dict], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(b)
    trs = [b[0]["high"] - b[0]["low"]] + [max(b[i]["high"] - b[i]["low"], abs(b[i]["high"] - b[i - 1]["close"]),
                                              abs(b[i]["low"] - b[i - 1]["close"])) for i in range(1, len(b))]
    if len(b) <= n:
        return out
    v = sum(trs[1:n + 1]) / n
    out[n] = v
    for i in range(n + 1, len(b)):
        v = (v * (n - 1) + trs[i]) / n
        out[i] = v
    return out


def sma_std(c: list[float], n: int) -> tuple[list[float | None], list[float | None]]:
    m: list[float | None] = [None] * len(c)
    s: list[float | None] = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        mu = sum(w) / n
        m[i] = mu
        s[i] = (sum((x - mu) ** 2 for x in w) / n) ** 0.5
    return m, s


def stoch(b: list[dict], n: int = 14, d: int = 3) -> tuple[list[float | None], list[float | None]]:
    k: list[float | None] = [None] * len(b)
    for i in range(n - 1, len(b)):
        hi = max(x["high"] for x in b[i - n + 1:i + 1])
        lo = min(x["low"] for x in b[i - n + 1:i + 1])
        k[i] = 100 * (b[i]["close"] - lo) / (hi - lo) if hi > lo else 50.0
    dd: list[float | None] = [None] * len(b)
    for i in range(n - 1 + d - 1, len(b)):
        dd[i] = sum(k[i - d + 1:i + 1]) / d  # type: ignore[arg-type]
    return k, dd


# ---------------------------------------------------------------- engine
class Ctx:
    def __init__(self, bars: list[dict]):
        self.b = bars
        c = [x["close"] for x in bars]
        self.c = c
        self.atr = atr(bars)
        self.rsi2, self.rsi14 = rsi(c, 2), rsi(c, 14)
        self.bbm, self.bbs = sma_std(c, 20)
        self.k, self.d = stoch(bars)
        self.e9, self.e21 = ema(c, 9), ema(c, 21)
        e12, e26 = ema(c, 12), ema(c, 26)
        self.macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
        first = next(i for i, v in enumerate(self.macd) if v is not None)
        sig = ema([v for v in self.macd[first:]], 9)  # type: ignore[misc]
        self.sig = [None] * first + sig
        self.hm = [((x["time"] // 3600) % 24, (x["time"] // 60) % 60) for x in bars]  # server clock of bar open
        self.wd = [datetime.fromtimestamp(x["time"], tz=timezone.utc).weekday() for x in bars]
        self.day = [x["time"] // 86400 for x in bars]


def mins(ctx: Ctx, i: int) -> int:
    """Server minutes since midnight at the CLOSE of bar i."""
    h, m = ctx.hm[i]
    return (h * 60 + m + 5) % 1440


Signal = Callable[[Ctx, int, dict], str | None]  # returns "buy" / "sell" / None at the close of bar i


def x_up(a, b, i):
    return a[i] is not None and b[i] is not None and a[i - 1] is not None and b[i - 1] is not None \
        and a[i - 1] <= b[i - 1] and a[i] > b[i]


def x_dn(a, b, i):
    return a[i] is not None and b[i] is not None and a[i - 1] is not None and b[i - 1] is not None \
        and a[i - 1] >= b[i - 1] and a[i] < b[i]


def s_rsi2(c, i, st_):
    v = c.rsi2[i]
    return None if v is None else "buy" if v < 5 else "sell" if v > 95 else None


def s_rsi14(c, i, st_):
    a, b = c.rsi14[i - 1], c.rsi14[i]
    if a is None or b is None:
        return None
    return "buy" if a < 30 <= b else "sell" if a > 70 >= b else None


def s_bb(c, i, st_):
    if c.bbm[i - 1] is None:
        return None
    up0, lo0 = c.bbm[i - 1] + 2 * c.bbs[i - 1], c.bbm[i - 1] - 2 * c.bbs[i - 1]
    up1, lo1 = c.bbm[i] + 2 * c.bbs[i], c.bbm[i] - 2 * c.bbs[i]
    if c.c[i - 1] < lo0 and c.c[i] > lo1:
        return "buy"
    if c.c[i - 1] > up0 and c.c[i] < up1:
        return "sell"
    return None


def s_stoch(c, i, st_):
    if c.k[i] is None or c.d[i] is None or c.k[i - 1] is None or c.d[i - 1] is None:
        return None
    if x_up(c.k, c.d, i) and c.k[i] < 20:
        return "buy"
    if x_dn(c.k, c.d, i) and c.k[i] > 80:
        return "sell"
    return None


def s_ema(c, i, st_):
    return "buy" if x_up(c.e9, c.e21, i) else "sell" if x_dn(c.e9, c.e21, i) else None


def s_macd(c, i, st_):
    return "buy" if x_up(c.macd, c.sig, i) else "sell" if x_dn(c.macd, c.sig, i) else None


def s_don(c, i, st_):
    if i < 21:
        return None
    hi = max(x["high"] for x in c.b[i - 20:i])
    lo = min(x["low"] for x in c.b[i - 20:i])
    return "buy" if c.c[i] > hi else "sell" if c.c[i] < lo else None


def range_break(start: int, end: int, until: int) -> Signal:
    """Range = bars whose close time is in (start, end] server minutes; trade first close beyond it before `until`."""
    def f(c: Ctx, i: int, st_: dict) -> str | None:
        t = mins(c, i)
        day = c.day[i] if t > 0 else c.day[i] - 1
        if st_.get("day") != day:
            st_.clear()
            st_["day"] = day
        if start < t <= end:
            b = c.b[i]
            st_["hi"] = max(st_.get("hi", b["high"]), b["high"])
            st_["lo"] = min(st_.get("lo", b["low"]), b["low"])
            return None
        if end < t <= until and "hi" in st_ and not st_.get("done"):
            if c.c[i] > st_["hi"]:
                st_["done"] = True
                return "buy"
            if c.c[i] < st_["lo"]:
                st_["done"] = True
                return "sell"
        return None
    return f


def at_time(t_entry: int, side: str) -> Signal:
    return lambda c, i, st_: side if mins(c, i) == t_entry else None


def s_ny_mom(c, i, st_):
    t = mins(c, i)
    if t == 16 * 60 + 30:
        st_["open"] = c.c[i]
    if t == 17 * 60 and "open" in st_:
        side = "buy" if c.c[i] > st_["open"] else "sell" if c.c[i] < st_["open"] else None
        st_.pop("open")
        return side
    return None


def news_reaction(release: int, k: float = 1.5) -> Signal:
    """release = server minute of the release; decide 15 minutes later on the first move vs ATR before release."""
    def f(c: Ctx, i: int, st_: dict) -> str | None:
        t = mins(c, i)
        if t == release:
            st_["px"], st_["atr"], st_["day"] = c.c[i], c.atr[i], c.day[i]
        elif t == release + 15 and st_.get("day") == c.day[i] and st_.get("atr"):
            move = c.c[i] - st_["px"]
            st_.clear()
            if abs(move) >= k * c.atr[i - 3]:  # ATR of the bar that closed at the release time
                return "buy" if move > 0 else "sell"
        return None
    return f


# name: (signal, stop_atr, target_atr or None, max_hold_bars or None, exit_at_server_minute or None)
TECHNIQUES: dict[str, tuple] = {
    "RSI2": (s_rsi2, 1.5, 1.0, 12, None),
    "RSI14": (s_rsi14, 1.5, 1.0, 12, None),
    "BB20": (s_bb, 1.5, 1.0, 12, None),
    "STOCH": (s_stoch, 1.5, 1.0, 12, None),
    "EMA9_21": (s_ema, 1.5, 3.0, 48, None),
    "MACD": (s_macd, 1.5, 3.0, 48, None),
    "DON20": (s_don, 1.5, 3.0, 48, None),
    "ORB_LON": (range_break(10 * 60, 10 * 60 + 30, 14 * 60), 1.5, 3.0, 48, None),
    "ORB_NY": (range_break(16 * 60 + 30, 17 * 60, 20 * 60), 1.5, 3.0, 48, None),
    "ASIA_BRK": (range_break(60, 10 * 60, 14 * 60), 1.5, 3.0, 48, None),
    "ASIA_LONG": (at_time(65, "buy"), 3.5, None, None, 10 * 60),
    "DAY_SHORT": (at_time(10 * 60, "sell"), 3.5, None, None, 22 * 60),
    "NY_MOM": (s_ny_mom, 3.5, None, None, 22 * 60 + 30),
    "NEWS0830_2H": (news_reaction(15 * 60 + 30), 3.5, None, None, 17 * 60 + 45),
    "NEWS0830_4H": (news_reaction(15 * 60 + 30), 3.5, None, None, 19 * 60 + 45),
    "NEWS1000_2H": (news_reaction(17 * 60), 3.5, None, None, 19 * 60 + 15),
    "NEWS1400_2H": (news_reaction(21 * 60), 3.5, None, None, 23 * 60 + 15),
}


def run(ctx: Ctx, name: str, costs: bool = True) -> list[dict]:
    sig_fn, k_sl, k_tp, hold, exit_t = TECHNIQUES[name]
    b = ctx.b
    trades: list[dict] = []
    pos = None
    state: dict = {}
    for i in range(60, len(b) - 1):
        spr = max(b[i + 1]["spread"], 55) * POINT if costs else 0.0
        slip = SLIP if costs else 0.0
        if pos:  # manage on bar i (entered at an earlier open)
            bar = b[i]
            done = None
            if pos["side"] == "buy":
                if bar["low"] <= pos["sl"]:
                    done = (min(pos["sl"], bar["open"]) - pos["slip"], "sl")
                elif pos["tp"] is not None and bar["high"] >= pos["tp"]:
                    done = (pos["tp"], "tp")
            else:
                if bar["high"] + pos["spr"] >= pos["sl"]:
                    done = (max(pos["sl"], bar["open"] + pos["spr"]) + pos["slip"], "sl")
                elif pos["tp"] is not None and bar["low"] + pos["spr"] <= pos["tp"]:
                    done = (pos["tp"], "tp")
            if not done:
                timed = (hold and i - pos["i"] + 1 >= hold) or (exit_t is not None and mins(ctx, i) == exit_t) \
                    or ctx.wd[i] == 4 and mins(ctx, i) >= 22 * 60
                if timed:
                    o = b[i + 1]["open"]
                    done = (o - slip if pos["side"] == "buy" else o + spr + slip, "time")
            if done:
                px, why = done
                pnl = (px - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - px)
                trades.append({"t": b[pos["i"]]["time"], "side": pos["side"], "r": pnl / pos["risk"], "why": why})
                pos = None
                if why == "time":
                    continue
        side = sig_fn(ctx, i, state)
        if pos or not side or ctx.atr[i] is None:
            continue
        if ctx.wd[i] == 4 and mins(ctx, i) >= 21 * 60 or ctx.wd[i] >= 5:
            continue
        o = b[i + 1]["open"]
        a = ctx.atr[i]
        entry = o + spr + slip if side == "buy" else o - slip
        risk = k_sl * a
        sl = entry - risk if side == "buy" else entry + risk
        tp = None if k_tp is None else (entry + k_tp * a if side == "buy" else entry - k_tp * a)
        pos = {"side": side, "entry": entry, "sl": sl, "tp": tp, "risk": risk, "i": i + 1, "spr": spr, "slip": slip}
    return trades


def summary(tr: list[dict]) -> dict:
    if len(tr) < 3:
        return {"n": len(tr)}
    r = [t["r"] for t in tr]
    mu, sd = st.mean(r), st.stdev(r)
    return {"n": len(r), "avg_r": mu, "t": mu / sd * len(r) ** 0.5 if sd else 0.0,
            "win": sum(1 for x in r if x > 0) / len(r), "sum_r": sum(r)}


def main() -> None:
    bars = load(DATA_DIR / "GOLD_M5.csv")
    ctx = Ctx(bars)
    split = bars[int(len(bars) * 0.7)]["time"]
    days = (bars[-1]["time"] - bars[0]["time"]) / 86400 * 5 / 7
    print(f"data {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc):%Y-%m-%d}.."
          f"{datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc):%Y-%m-%d}, ~{days:.0f} trading days")
    print(f"{'technique':10s}    n  /day  win   avgR    IS     OOS    t     no-cost  PASS")
    out, passed = {}, []
    for name in TECHNIQUES:
        tr = run(ctx, name)
        s = summary(tr)
        if s["n"] < 3:
            print(f"{name:10s} {s['n']:5d}  too few")
            continue
        is_, oos = summary([t for t in tr if t["t"] < split]), summary([t for t in tr if t["t"] >= split])
        nc = summary(run(ctx, name, costs=False))
        ok = s["n"] >= (60 if name.startswith("NEWS") else 200) and is_.get("avg_r", -1) > 0 and oos.get("avg_r", -1) > 0 and s["t"] >= 2
        if ok:
            passed.append(name)
        out[name] = {"all": s, "is": is_, "oos": oos, "no_cost": nc, "pass": ok}
        print(f"{name:10s} {s['n']:5d} {s['n'] / days:5.1f} {s['win']:.0%} {s['avg_r']:+.3f} "
              f"{is_.get('avg_r', 0):+.3f} {oos.get('avg_r', 0):+.3f} {s['t']:+5.2f}  {nc.get('avg_r', 0):+.3f}   "
              f"{'PASS' if ok else '-'}")
    print(f"\nPASS: {passed or 'none'}")
    if passed:
        allt = [t for n in passed for t in run(ctx, n)]
        s = summary(allt)
        print(f"portfolio of passes: {s['n']} trades, {s['n'] / days:.1f}/day, avg R {s['avg_r']:+.3f}, t {s['t']:.2f}")
    path = Path(__file__).resolve().parent.parent / "exports" / "techniques.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
