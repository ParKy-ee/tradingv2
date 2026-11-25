"""Backtest of the playbook setups on broker history, with spread, slippage and conservative fills.

This is a deterministic *approximation* of what the LLM agent is told to do (strategy/playbook.md). It does not
model the agent's discretion (early closes, cancels on invalidation, news). Its job is to answer: does each setup
have any edge after real costs, and do the v2 filters help?

    python -m xau_agent.backtest                 # all setups, M5 fills, full history + M1-fill check
Fill rules (bid OHLC bars, ask = bid + spread):
- limit orders fill at their price (or better on a gap), stop orders at price + slippage;
- on the fill bar only the SL can be hit; if SL and TP fall in the same bar, SL is assumed first;
- SL exits pay slippage, TP exits do not.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .history import DATA_DIR, load

POINT = 0.01
USD_PER_POINT_001LOT = 0.01  # GOLD on XM: 0.01 lot = 1 oz -> $0.01 per point


# ---------------------------------------------------------------- incremental indicators per timeframe
class Ind:
    def __init__(self):
        self.n = 0
        self.closes: list[float] = []
        self.ema20 = self.ema50 = self.atr = None
        self._trs: list[float] = []
        self.prev_close = None

    def push(self, h: float, l: float, c: float) -> None:
        self.n += 1
        self.closes.append(c)
        if len(self.closes) > 60:
            self.closes.pop(0)
        for p, attr in ((20, "ema20"), (50, "ema50")):
            cur = getattr(self, attr)
            if cur is None:
                if self.n >= p:
                    setattr(self, attr, sum(self.closes[-p:]) / p)
            else:
                k = 2 / (p + 1)
                setattr(self, attr, c * k + cur * (1 - k))
        if self.prev_close is not None:
            tr = max(h - l, abs(h - self.prev_close), abs(l - self.prev_close))
            if self.atr is None:
                self._trs.append(tr)
                if len(self._trs) == 14:
                    self.atr = sum(self._trs) / 14
            else:
                self.atr = (self.atr * 13 + tr) / 14
        self.prev_close = c

    def ready(self) -> bool:
        return self.ema50 is not None and bool(self.atr)

    def trend(self) -> str | None:
        """Same rule as indicators.trend: EMA gap in ATR units, ±0.1 = flat, plus price vs EMA50."""
        if not self.ready():
            return None
        gap = (self.ema20 - self.ema50) / self.atr
        c = self.closes[-1]
        if gap > 0.1 and c > self.ema50:
            return "up"
        if gap < -0.1 and c < self.ema50:
            return "down"
        return "flat"


class Agg:
    """Builds a higher timeframe from M5 bars; pushes a bar into Ind only when the bucket is complete."""

    def __init__(self, sec: int):
        self.sec, self.ind, self.cur = sec, Ind(), None

    def add(self, b: dict) -> None:
        key = b["time"] // self.sec
        if self.cur and self.cur[0] != key:
            self._close()
        if not self.cur:
            self.cur = [key, b["high"], b["low"], b["close"]]
        else:
            self.cur[1] = max(self.cur[1], b["high"])
            self.cur[2] = min(self.cur[2], b["low"])
            self.cur[3] = b["close"]
        if b["time"] + 300 >= (key + 1) * self.sec:
            self._close()

    def _close(self) -> None:
        _, h, l, c = self.cur
        self.ind.push(h, l, c)
        self.cur = None


# ---------------------------------------------------------------- market state seen at a decision
@dataclass
class State:
    t: int  # decision time (server epoch) = M5 bar close
    bar: dict
    m5: Ind
    tf: dict[str, Ind]
    hi: float
    lo: float
    recent: list[dict]  # last N M5 bars
    spread: float  # price units
    hist: list[dict] = field(default_factory=list)  # last 60 M5 bars (for regime filters)

    def trend(self, name: str) -> str | None:
        return self.m5.trend() if name == "M5" else self.tf[name].trend()

    def htf_bias(self) -> str | None:
        ts = {self.trend(x) for x in ("M15", "M30", "H1")}
        return ts.pop() if len(ts) == 1 and ts <= {"up", "down"} else None


@dataclass
class Order:
    setup: str
    side: str
    kind: str  # limit | stop | market
    price: float
    sl: float
    tp: float
    expires: int
    placed: int
    max_hold_min: int = 0  # 0 = no time exit


@dataclass
class Trade:
    setup: str
    side: str
    kind: str
    placed: int
    filled: int
    closed: int
    entry: float
    sl: float
    tp: float
    exit: float
    exit_reason: str
    points: float = 0.0
    r: float = 0.0


# ---------------------------------------------------------------- setups (deterministic playbook)
def _rr_ok(entry, sl, tp, min_rr=1.2):
    return abs(entry - sl) > 0 and abs(tp - entry) / abs(entry - sl) >= min_rr


def _sl_ok(entry, sl, min_pts=50, max_pts=1200):
    d = abs(entry - sl) / POINT
    return min_pts <= d <= max_pts


def range_fade(s: State, v2: bool) -> Order | None:
    a = s.m5.atr
    # RANGE context: higher timeframes not both trending the same way
    if s.trend("M15") == s.trend("H1") and s.trend("M15") in ("up", "down"):
        return None
    width = s.hi - s.lo
    if not (2 * a <= width <= 8 * a):
        return None
    c = s.bar["close"]
    third = width / 3
    mid = (s.hi + s.lo) / 2
    sides = []
    if c <= s.lo + third:
        sides.append("buy")
    if c >= s.hi - third:
        sides.append("sell")
    for side in sides:
        if v2:
            m15 = s.tf["M15"]
            gap = (m15.ema20 - m15.ema50) / m15.atr
            if gap < -0.1 and side == "buy":
                continue
            if gap > 0.1 and side == "sell":
                continue
            b = s.bar
            if side == "buy" and not (b["low"] <= s.lo + 0.1 * a and b["close"] > s.lo + 0.1 * a):
                continue
            if side == "sell" and not (b["high"] >= s.hi - 0.1 * a and b["close"] < s.hi - 0.1 * a):
                continue
            edge = s.lo if side == "buy" else s.hi
            tests = sum(1 for x in s.recent[-24:]
                        if (x["low"] <= edge + 0.15 * a if side == "buy" else x["high"] >= edge - 0.15 * a))
            if tests >= 4:  # this bar + 3 earlier tests
                continue
        if side == "buy":
            entry, sl, tp = s.lo + 0.15 * a, s.lo - 0.5 * a, mid
            if not entry < c + s.spread:
                continue
        else:
            entry, sl, tp = s.hi - 0.15 * a, s.hi + 0.5 * a, mid
            if not entry > c:
                continue
        if _rr_ok(entry, sl, tp) and _sl_ok(entry, sl):
            return Order("RANGE_FADE_v2" if v2 else "RANGE_FADE_v1", side, "limit", entry, sl, tp,
                         s.t + 30 * 60, s.t)
    return None


def breakout(s: State) -> Order | None:
    a = s.m5.atr
    h1 = s.trend("H1")
    c = s.bar["close"]
    if h1 == "up" and s.hi - c <= 0.3 * a:
        entry = s.hi + 1.5 * s.spread
        sl = entry - 0.9 * a
        tp = entry + 1.5 * (entry - sl)
        side = "buy"
    elif h1 == "down" and c - s.lo <= 0.3 * a:
        entry = s.lo - 1.5 * s.spread
        sl = entry + 0.9 * a
        tp = entry - 1.5 * (sl - entry)
        side = "sell"
    else:
        return None
    if not _sl_ok(entry, sl):
        return None
    return Order("BREAKOUT", side, "stop", entry, sl, tp, s.t + 40 * 60, s.t)


def pullback(s: State) -> Order | None:
    a = s.m5.atr
    m15, h1 = s.trend("M15"), s.trend("H1")
    if m15 != h1 or m15 not in ("up", "down"):
        return None
    c, e20 = s.bar["close"], s.m5.ema20
    if m15 == "up":
        if not (c > e20 and c - e20 <= 1.0 * a):
            return None
        entry = e20
        swing = min(x["low"] for x in s.recent[-12:])
        sl = min(max(swing, entry - 1.2 * a), entry - 0.6 * a)
        tp = entry + 1.5 * (entry - sl)
        side = "buy"
    else:
        if not (c < e20 and e20 - c <= 1.0 * a):
            return None
        entry = e20
        swing = max(x["high"] for x in s.recent[-12:])
        sl = max(min(swing, entry + 1.2 * a), entry + 0.6 * a)
        tp = entry - 1.5 * (sl - entry)
        side = "sell"
    if not _sl_ok(entry, sl):
        return None
    return Order("PULLBACK", side, "limit", entry, sl, tp, s.t + 30 * 60, s.t)


SETUPS: dict[str, Callable[[State], Order | None]] = {
    "RANGE_FADE_v1": lambda s: range_fade(s, False),
    "RANGE_FADE_v2": lambda s: range_fade(s, True),
    "BREAKOUT": breakout,
    "PULLBACK": pullback,
}


# ---------------------------------------------------------------- engine
@dataclass
class Config:
    spread_points: float | None = None  # None = use the bar's recorded spread (min 55)
    slippage_points: float = 10
    range_bars: int = 30  # ~2.5 hours of M5
    session_server_hours: tuple[int, int] = (3, 22)  # ≈ 01:00-20:00 UTC on a UTC+2/+3 server
    htf_gate: bool = True
    fills: str = "m5"  # m5 | m1
    start: int = 0
    end: int = 2 ** 40


def _spread(cfg: Config, bar: dict) -> float:
    if cfg.spread_points is not None:
        return cfg.spread_points * POINT
    return max(bar.get("spread") or 0, 55) * POINT


class Engine:
    def __init__(self, setup_fns: list[Callable[[State], Order | None]], cfg: Config):
        self.fns, self.cfg = setup_fns, cfg
        self.trades: list[Trade] = []
        self.order: Order | None = None
        self.pos: dict | None = None
        self.placed = 0
        self.gated = 0

    # -- fills on one bar of the fill series
    def _on_bar(self, b: dict) -> None:
        cfg = self.cfg
        spr = _spread(cfg, b)
        slip = cfg.slippage_points * POINT
        if self.order and b["time"] >= self.order.expires:
            self.order = None
        if self.order:
            o = self.order
            ask_o, ask_h, ask_l = b["open"] + spr, b["high"] + spr, b["low"] + spr
            fill = None
            if o.kind == "market":
                fill = ask_o + slip if o.side == "buy" else b["open"] - slip
            elif o.side == "buy" and o.kind == "limit" and ask_l <= o.price:
                fill = min(o.price, ask_o)
            elif o.side == "buy" and o.kind == "stop" and ask_h >= o.price:
                fill = max(o.price, ask_o) + slip
            elif o.side == "sell" and o.kind == "limit" and b["high"] >= o.price:
                fill = max(o.price, b["open"])
            elif o.side == "sell" and o.kind == "stop" and b["low"] <= o.price:
                fill = min(o.price, b["open"]) - slip
            if fill is not None:
                self.pos = {"o": o, "entry": fill, "filled": b["time"]}
                self.order = None
                self._exits(b, spr, slip, fill_bar=True)
                return
        if self.pos:
            o = self.pos["o"]
            if o.max_hold_min and b["time"] >= self.pos["filled"] + o.max_hold_min * 60:
                px = b["open"] - slip if o.side == "buy" else b["open"] + spr + slip
                return self._close(b, px, "time")
            self._exits(b, spr, slip, fill_bar=False)

    def _exits(self, b, spr, slip, fill_bar: bool) -> None:
        p = self.pos
        o = p["o"]
        if o.side == "buy":
            sl_hit = b["low"] <= o.sl
            tp_hit = (not fill_bar) and b["high"] >= o.tp
            if sl_hit:
                return self._close(b, min(o.sl, b["open"]) - slip if not fill_bar else o.sl - slip, "sl")
            if tp_hit:
                return self._close(b, o.tp, "tp")
        else:
            sl_hit = b["high"] + spr >= o.sl
            tp_hit = (not fill_bar) and b["low"] + spr <= o.tp
            if sl_hit:
                return self._close(b, max(o.sl, b["open"] + spr) + slip if not fill_bar else o.sl + slip, "sl")
            if tp_hit:
                return self._close(b, o.tp, "tp")

    def _close(self, b, price, reason) -> None:
        p = self.pos
        o = p["o"]
        pts = ((price - p["entry"]) if o.side == "buy" else (p["entry"] - price)) / POINT
        risk = abs(p["entry"] - o.sl) / POINT
        self.trades.append(Trade(o.setup, o.side, o.kind, o.placed, p["filled"], b["time"], p["entry"], o.sl, o.tp,
                                 price, reason, round(pts, 1), round(pts / risk, 3) if risk else 0.0))
        self.pos = None

    # -- decision at an M5 close
    def decide(self, s: State) -> None:
        if self.pos or self.order:
            return
        h = datetime.fromtimestamp(s.t, tz=timezone.utc).hour
        lo, hi = self.cfg.session_server_hours
        if not lo <= h < hi or datetime.fromtimestamp(s.t, tz=timezone.utc).weekday() >= 5:
            return
        for fn in self.fns:
            o = fn(s)
            if not o:
                continue
            bias = s.htf_bias()
            if self.cfg.htf_gate and bias and o.side != ("buy" if bias == "up" else "sell"):
                self.gated += 1
                continue
            self.order = o
            self.placed += 1
            return


def run(setup_names: list[str], cfg: Config, m5: list[dict], m1: list[dict] | None = None) -> dict[str, Any]:
    eng = Engine([SETUPS[n] for n in setup_names], cfg)
    m5ind = Ind()
    tfs = {"M15": Agg(900), "M30": Agg(1800), "H1": Agg(3600)}
    recent: deque = deque(maxlen=cfg.range_bars)
    hist: deque = deque(maxlen=60)
    j = 0  # pointer into m1
    for i, b in enumerate(m5):
        m5ind.push(b["high"], b["low"], b["close"])
        for a in tfs.values():
            a.add(b)
        recent.append(b)
        hist.append(b)
        t_close = b["time"] + 300
        if not (cfg.start <= b["time"] < cfg.end):
            continue
        if m5ind.ready() and all(a.ind.ready() for a in tfs.values()) and len(recent) == cfg.range_bars:
            rb = list(recent)
            s = State(t_close, b, m5ind, {k: a.ind for k, a in tfs.items()},
                      max(x["high"] for x in rb), min(x["low"] for x in rb), rb, _spread(cfg, b), list(hist))
            eng.decide(s)
        # fills during the next 5 minutes
        if cfg.fills == "m1" and m1:
            while j < len(m1) and m1[j]["time"] < t_close:
                j += 1
            while j < len(m1) and m1[j]["time"] < t_close + 300:
                eng._on_bar(m1[j])
                j += 1
        elif i + 1 < len(m5):
            eng._on_bar(m5[i + 1])
    return {"trades": eng.trades, "placed": eng.placed, "gated": eng.gated}


# ---------------------------------------------------------------- metrics
def metrics(trades: list[Trade]) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0}
    pts = [t.points for t in trades]
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    eq = peak = dd = 0.0
    for p in pts:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 3),
        "net_points": round(sum(pts), 1),
        "net_usd_0.01lot": round(sum(pts) * USD_PER_POINT_001LOT, 2),
        "avg_points": round(sum(pts) / n, 1),
        "avg_r": round(sum(t.r for t in trades) / n, 3),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_drawdown_points": round(dd, 1),
        "exits": {k: sum(1 for t in trades if t.exit_reason == k) for k in ("tp", "sl", "time")},
    }


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "GOLD"
    m5 = load(DATA_DIR / f"{symbol}_M5.csv")
    m1_path = DATA_DIR / f"{symbol}_M1.csv"
    m1 = load(m1_path) if m1_path.exists() else []
    split = m5[int(len(m5) * 0.7)]["time"]
    out: dict[str, Any] = {
        "data": {"m5": [_date(m5[0]["time"]), _date(m5[-1]["time"]), len(m5)],
                 "m1": [_date(m1[0]["time"]), _date(m1[-1]["time"]), len(m1)] if m1 else None,
                 "in_sample_until": _date(split)},
        "results": {},
    }
    report = []
    for name in SETUPS:
        res: dict[str, Any] = {}
        for label, cfg in (
            ("full", Config()),
            ("in_sample", Config(end=split)),
            ("out_of_sample", Config(start=split)),
            ("full_no_costs", Config(spread_points=0, slippage_points=0)),
            ("full_no_htf_gate", Config(htf_gate=False)),
        ):
            r = run([name], cfg, m5)
            res[label] = {**metrics(r["trades"]), "placed": r["placed"], "gated": r["gated"]}
        if m1:
            start = m1[0]["time"] + 86400
            r5 = run([name], Config(start=start), m5)
            r1 = run([name], Config(start=start, fills="m1"), m5, m1)
            res["m1_period_m5_fills"] = metrics(r5["trades"])
            res["m1_period_m1_fills"] = metrics(r1["trades"])
        out["results"][name] = res
        f = res["full"]
        report.append(f"{name:14s} trades {f.get('trades', 0):5d}  win {f.get('win_rate', 0):.0%}  "
                      f"avgR {f.get('avg_r', 0):+.3f}  net ${f.get('net_usd_0.01lot', 0):+.2f}  "
                      f"PF {f.get('profit_factor')}  | OOS avgR {res['out_of_sample'].get('avg_r', 0):+.3f}  "
                      f"| no-cost avgR {res['full_no_costs'].get('avg_r', 0):+.3f}")
    path = Path(__file__).resolve().parent.parent / "exports" / "backtest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["data"]))
    print("\n".join(report))
    print(f"full report -> {path}")


if __name__ == "__main__":
    main()
