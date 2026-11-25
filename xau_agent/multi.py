"""Does the H4 Donchian 20/10 swing rule also work on other XM symbols? (forward-test candidates)

Everything below was fixed BEFORE looking at any result:
- Symbols: SILVER, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, US500Cash, US100Cash, US30Cash, GER40Cash,
  JP225Cash, UK100Cash, OILCash, BTCUSD; GOLD as the reference.
- Rule: exactly the gold rule (xau_agent/swing.py DON20_10): close beyond the prior 20-bar channel -> enter next
  open, 2x ATR20 broker stop, exit on a close beyond the prior 10-bar channel. No parameter changes per symbol.
- Data: up to 50,000 H4 bars from the XM terminal, only from the first year in which bars are really 4-hourly
  (>= 4 bars per trading day; earlier XM "H4" history is daily data).
- Costs, scaled with price like the gold test: spread = median recorded spread of the last 3,000 bars (or the
  current spread if none recorded), slippage = 20% of spread, swap = today's XM swap per night (points or money
  per lot -> price units), triple on Wednesday.
- Baseline: BASE20_10 (enter whenever flat on the same side, same stop and exit), as in the gold test.
- PASS only if all hold: n >= 100; avg R (both sides together) >= +0.05; avg R > 0 in both halves of the
  symbol's period; and avg R beats the baseline's (both sides together).
- Multiple testing: 14 symbols are tested, so 1 or 2 may pass by luck. A PASS makes a symbol a forward-test
  candidate on DEMO, not proof of edge.

    python -m xau_agent.multi
    python -m xau_agent.multi --tf H1 --symbols GOLD,BTCUSD

H1 test (declared 2026-11-25 17:15, before running): same rule, bar counts and ATR multiple unchanged (so the
channel spans 20 hours instead of 80); same costs model; real-H1 period only (>= 12 bars per trading day).
PASS needs everything above AND avg R > 0 on the last 30% of trades (out-of-sample style holdout) AND t >= 2.
A PASS would make H1 a forward-test candidate alongside H4 (more trades); a FAIL keeps H4 only.
"""
from __future__ import annotations

import csv
import statistics as st
from datetime import datetime, timezone

from .history import DATA_DIR
from .swing import backtest, stats

SYMBOLS = ["GOLD", "SILVER", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "US500Cash", "US100Cash",
           "US30Cash", "GER40Cash", "JP225Cash", "UK100Cash", "OILCash", "BTCUSD"]


def fetch(symbol: str, tf: str = "H4") -> tuple[list[dict], dict]:
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise RuntimeError(mt5.last_error())
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    r = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), 0, 50000)
    if r is None or not len(r):
        raise RuntimeError(f"{symbol}: no {tf} data {mt5.last_error()}")
    bars = [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]),
             "close": float(x["close"]), "spread": int(x["spread"])} for x in r][:-1]  # drop the forming bar
    path = DATA_DIR / f"{symbol}_{tf}.csv"
    DATA_DIR.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(bars[0]))
        w.writeheader()
        w.writerows(bars)
    meta = {"point": info.point, "spread_now": info.spread, "swap_mode": info.swap_mode,
            "swap_long": info.swap_long, "swap_short": info.swap_short, "contract": info.trade_contract_size}
    return bars, meta


def real_h4_start(bars: list[dict], per_day: float = 4) -> int:
    """First calendar year whose bars average >= per_day per trading day."""
    by_year: dict[int, list[int]] = {}
    for b in bars:
        by_year.setdefault(datetime.fromtimestamp(b["time"], tz=timezone.utc).year, []).append(b["time"])
    for y in sorted(by_year):
        days = {t // 86400 for t in by_year[y]}
        if len(by_year[y]) / max(1, len(days)) >= per_day:
            return int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())
    return bars[-1]["time"]


def costs_for(bars: list[dict], meta: dict) -> tuple[tuple[float, float, float, float], dict]:
    price = bars[-1]["close"]
    recorded = [b["spread"] for b in bars[-3000:] if b["spread"] > 0]
    spread_pts = st.median(recorded) if recorded else meta["spread_now"]
    spread = spread_pts * meta["point"]
    if spread <= 0:
        spread = 0.0002 * price  # no spread information at all: assume 2 bp
    if meta["swap_mode"] == 1:  # points
        sl, ss = meta["swap_long"] * meta["point"], meta["swap_short"] * meta["point"]
    elif meta["swap_mode"] in (2, 3, 4):  # money per lot -> price units per 1 unit of contract (USD-ish)
        sl, ss = meta["swap_long"] / meta["contract"], meta["swap_short"] / meta["contract"]
    else:
        sl = ss = 0.0
    c = (spread / price, 0.2 * spread / price, sl / price, ss / price)
    return c, {"price": price, "spread_pts": spread_pts, "spread_bp": round(spread / price * 1e4, 2),
               "swap_long_bp": round(sl / price * 1e4, 3), "swap_short_bp": round(ss / price * 1e4, 3)}


def evaluate(symbol: str, tf: str = "H4") -> dict:
    bars, meta = fetch(symbol, tf)
    t0 = real_h4_start(bars, 4 if tf == "H4" else 12)
    if symbol == "GOLD" and tf == "H4":
        t0 = max(t0, int(datetime(2013, 6, 10, tzinfo=timezone.utc).timestamp()))  # as in the gold test
    costs, cinfo = costs_for(bars, meta)
    don = backtest(bars, "DON20_10", ("buy", "sell"), t0, costs)
    don = sorted(don, key=lambda t: t["t0"])
    base = (backtest(bars, "BASE20_10", ("buy",), t0, costs) + backtest(bars, "BASE20_10", ("sell",), t0, costs))
    s, sb = stats(don), stats(base)
    mid = t0 + (bars[-1]["time"] - t0) // 2
    h1, h2 = stats([t for t in don if t["t0"] < mid]), stats([t for t in don if t["t0"] >= mid])
    by_side = {sd: stats([t for t in don if t["side"] == sd]) for sd in ("buy", "sell")}
    import statistics as _st
    rs = [t["r"] for t in don]
    tstat = (_st.mean(rs) / _st.stdev(rs) * len(rs) ** 0.5) if len(rs) > 2 else 0.0
    hold = stats(don[int(len(don) * 0.7):])
    ok = (s.get("n", 0) >= 100 and s.get("avg_r", -1) >= 0.05 and h1.get("avg_r", -1) > 0
          and h2.get("avg_r", -1) > 0 and s.get("avg_r", -1) > sb.get("avg_r", 0))
    years = (bars[-1]["time"] - t0) / (365.25 * 86400)
    return {"symbol": symbol, "from": datetime.fromtimestamp(t0, tz=timezone.utc).year, "years": round(years, 1),
            "n": s.get("n", 0), "per_year": round(s.get("n", 0) / years, 1) if years > 0 else 0,
            "win": s.get("win"), "avg_r": s.get("avg_r"), "pf": s.get("pf"), "h1": h1.get("avg_r"),
            "h2": h2.get("avg_r"), "base": sb.get("avg_r"), "buy": by_side["buy"].get("avg_r"),
            "sell": by_side["sell"].get("avg_r"), "t": round(tstat, 2), "hold": hold.get("avg_r"),
            "pass": ok and (tf == "H4" or ((hold.get("avg_r") or -1) > 0 and tstat >= 2)), **cinfo}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="H4")
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    a = ap.parse_args()
    f = lambda v: "   -  " if v is None else f"{v:+.3f}"  # noqa: E731
    print(f"[{a.tf}] {'symbol':10s} from  yrs   n  n/yr  win   avgR   1st    2nd    base   buy    sell   last30%  t    spr_bp  PASS")
    rows = []
    for sym in a.symbols.split(","):
        try:
            r = evaluate(sym, a.tf)
        except Exception as exc:
            print(f"{sym:10s} ERROR {exc}")
            continue
        rows.append(r)
        pf = f"{r['pf']:.2f}" if r["pf"] else "-"
        print(f"{sym:10s} {r['from']} {r['years']:4.1f} {r['n']:4d} {r['per_year']:5.1f} "
              f"{(r['win'] or 0):.0%} {f(r['avg_r'])} {f(r['h1'])} {f(r['h2'])} {f(r['base'])} {f(r['buy'])} "
              f"{f(r['sell'])} {f(r['hold'])}  {r['t']:5.2f} {r['spread_bp']:6.2f}  {'PASS' if r['pass'] else '-'}  PF {pf}")
    passed = [r["symbol"] for r in rows if r["pass"]]
    print(f"\nPASS: {passed or 'none'}  ({len(passed)} of {len(rows)})")


if __name__ == "__main__":
    main()
