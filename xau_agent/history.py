"""Download broker price history to CSV for backtesting.

MT5 caps one request (terminal maxbars), so M5 is pulled in 30-day chunks going back until the broker has no
more data; M1 is pulled as the most recent ~99k bars. Times are broker server time (epoch seconds).

    python -m xau_agent.history            # writes data/<SYMBOL>_M5.csv and data/<SYMBOL>_M1.csv
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FIELDS = ("time", "open", "high", "low", "close", "tick_volume", "spread")


def _rows(rates) -> list[tuple]:
    return [tuple(r[f].item() if hasattr(r[f], "item") else r[f] for f in FIELDS) for r in rates]


def pull_m5(mt5, symbol: str, max_days: int = 1500) -> list[tuple]:
    end = datetime.now(timezone.utc)
    out: dict[int, tuple] = {}
    empty = 0
    for _ in range(max_days // 30):
        start = end - timedelta(days=30)
        r = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, start, end)
        rows = _rows(r) if r is not None else []
        rows = [x for x in rows if x[0] < int(end.timestamp()) + 86400 * 2]
        if len(rows) < 10:
            empty += 1
            if empty >= 2:
                break
        else:
            empty = 0
            out.update({x[0]: x for x in rows})
        end = start
    return [out[k] for k in sorted(out)]


def pull_m1(mt5, symbol: str, count: int = 99000) -> list[tuple]:
    r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, count)
    return _rows(r) if r is not None else []


def save(rows: list[tuple], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "spread": int(r["spread"])}
                for r in csv.DictReader(f)]


def main() -> None:
    from .broker import MT5Broker
    from .config import load_settings

    s = load_settings()
    b = MT5Broker(s.symbol, s.magic, s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_path)
    for tf, rows in (("M5", pull_m5(b.mt5, s.symbol)), ("M1", pull_m1(b.mt5, s.symbol))):
        path = DATA_DIR / f"{s.symbol}_{tf}.csv"
        save(rows, path)
        if rows:
            f = datetime.fromtimestamp(rows[0][0], tz=timezone.utc).date()
            t = datetime.fromtimestamp(rows[-1][0], tz=timezone.utc).date()
            print(f"{tf}: {len(rows)} bars {f} .. {t} -> {path}")
        else:
            print(f"{tf}: no data")


if __name__ == "__main__":
    main()
