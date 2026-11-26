"""Phase 0 of strategy/stock_plan.md: measure what US stock CFDs on XM really cost during US trading hours.

Sampler: every 60 s records bid/ask of the stock universe (logs/stock_universe.txt) whenever a symbol's tick is
fresh (market open), to logs/stockcost.csv. Once per full trading day (Mon–Thu), ~30 min after the open, it makes
one minimum-size DEMO round trip on three stocks to read the real commission and fill vs quote (magic 260520).

    python -m xau_agent.stockcost --until 2026-11-30T21:30     # UTC; run detached; pid in logs/stockcost.pid
    python -m xau_agent.stockcost --report

Pass thresholds (declared in stock_plan.md before measuring), median round-trip cost = spread + commission:
  A1 intraday ORB <= ~0.10%   |   A2 swing 1–7 days <= ~0.30%   |   B 1–3 months: carry cost judged separately.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics as st
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "logs" / "stockcost.csv"
PROBES = ROOT / "logs" / "stockcost_probes.csv"
PROBE_SYMBOLS = ["Apple", "Nvidia", "JPMorgan"]
MAGIC = 260520


def us_open_utc(d: datetime) -> datetime:
    """09:30 New York on day d, in UTC (EDT Mar–Nov, EST otherwise; uses the news module's DST rule)."""
    from .news import _ny_offset_hours
    off = _ny_offset_hours(d)  # -4 or -5
    return d.replace(hour=9 - off, minute=30, second=0, microsecond=0)


def probe(mt5, sym: str, settings) -> dict | None:
    """One minimum-size market buy and immediate close on DEMO; returns commission and slippage vs quote."""
    from .broker import MT5Broker
    from .main import guard_account
    b = MT5Broker(sym, MAGIC, settings.mt5_login, settings.mt5_password, settings.mt5_server, settings.mt5_path)
    guard_account(settings, b.account())  # refuses anything but a demo account (unless explicitly allowed)
    if b.account().trade_mode != "demo":
        return None
    spec, q = b.spec(), b.quote()
    r = b.send_order("buy", spec.volume_min, round(q.ask * 0.85, spec.digits), 0.0, "cost probe")
    if not r.ok:
        return {"symbol": sym, "error": f"open failed {r.retcode} {r.comment}"}
    time.sleep(2)
    q2 = b.quote()
    c = b.close_position(r.ticket)
    deals = mt5.history_deals_get(position=r.ticket) or []
    comm = sum(d.commission + getattr(d, "fee", 0.0) for d in deals)
    return {"symbol": sym, "ts": datetime.now(timezone.utc).isoformat(), "lot": spec.volume_min,
            "quote_ask_open": q.ask, "fill_open": r.price, "quote_bid_close": q2.bid, "fill_close": c.price,
            "commission": round(comm, 4), "net": round(sum(d.profit + d.commission + d.swap for d in deals), 4),
            "notional": round(r.price * spec.volume_min * 10, 2)}


def run(until: datetime) -> None:
    import MetaTrader5 as mt5
    from .config import load_settings
    from .status import keep_awake
    keep_awake()
    s = load_settings()
    mt5.initialize(path=s.mt5_path or None)
    (ROOT / "logs" / "stockcost.pid").write_text(str(os.getpid()))
    syms = [x.strip() for x in (ROOT / "logs" / "stock_universe.txt").read_text().splitlines() if x.strip()]
    for x in syms:
        mt5.symbol_select(x, True)
    last: dict[str, int] = {}
    probed_days: set[str] = set()
    new = not CSV.exists()
    with CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc", "symbol", "bid", "ask", "spread_bp", "min_since_open"])
        while datetime.now(timezone.utc) < until:
            now = datetime.now(timezone.utc)
            since = (now - us_open_utc(now)).total_seconds() / 60
            for x in syms:
                t = mt5.symbol_info_tick(x)
                if not t or t.bid <= 0 or t.ask <= 0 or last.get(x) == t.time_msc:
                    continue  # no fresh tick: market closed or symbol idle
                first = x not in last
                last[x] = t.time_msc
                if first:
                    continue  # the first tick seen may be a stale quote from the last session
                mid = (t.bid + t.ask) / 2
                w.writerow([now.isoformat(timespec="seconds"), x, t.bid, t.ask,
                            round((t.ask - t.bid) / mid * 1e4, 2), round(since, 1)])
            f.flush()
            day = now.strftime("%Y-%m-%d")
            if 30 <= since <= 45 and now.weekday() <= 3 and day not in probed_days and last:
                probed_days.add(day)
                new_p = not PROBES.exists()
                with PROBES.open("a", newline="", encoding="utf-8") as pf:
                    pw = csv.DictWriter(pf, fieldnames=["symbol", "ts", "lot", "quote_ask_open", "fill_open",
                                                        "quote_bid_close", "fill_close", "commission", "net",
                                                        "notional", "error"])
                    if new_p:
                        pw.writeheader()
                    for x in PROBE_SYMBOLS:
                        try:
                            res = probe(mt5, x, s)
                            if res:
                                pw.writerow(res)
                        except Exception as exc:  # a probe must never stop the sampler
                            pw.writerow({"symbol": x, "error": repr(exc)[:200]})
            time.sleep(60)


def report() -> None:
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    if not rows:
        print("no samples yet")
        return
    buckets = [("first 15 min", 0, 15), ("15-60 min", 15, 60), ("1h-5.5h", 60, 330), ("last 60 min", 330, 390)]
    days = sorted({r["utc"][:10] for r in rows})
    print(f"samples {len(rows)} on days {days}")
    by: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        m = float(r["min_since_open"])
        for name, a, b in buckets:
            if a <= m < b:
                by.setdefault(r["symbol"], {}).setdefault(name, []).append(float(r["spread_bp"]))
    allb: dict[str, list[float]] = {}
    print(f"{'symbol':16s}" + "".join(f"{n:>14s}" for n, _, _ in buckets) + "   (median spread, % of price)")
    for sym in sorted(by):
        line = f"{sym:16s}"
        for name, _, _ in buckets:
            v = by[sym].get(name, [])
            allb.setdefault(name, []).extend(v)
            line += f"{(st.median(v) / 100 if v else float('nan')):>13.3f}%"
        print(line)
    print("ALL" + " " * 13 + "".join(f"{(st.median(allb[n]) / 100 if allb.get(n) else float('nan')):>13.3f}%"
                                      for n, _, _ in buckets))
    if PROBES.exists():
        print("\nDEMO round-trip probes:")
        for p in csv.DictReader(PROBES.open(encoding="utf-8")):
            print(p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        return report()
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else \
        datetime.now(timezone.utc).replace(hour=23)
    run(until)


if __name__ == "__main__":
    main()
