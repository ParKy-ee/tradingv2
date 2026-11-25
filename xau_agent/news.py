"""Economic calendar for gold: store upcoming USD events, block entries around high-impact ones.

Source: ForexFactory's weekly JSON (free, unofficial; may change or disappear). Fields: title, country, date
(ISO with offset), impact, forecast, previous. It has no actual values; the market reaction is measured from
our own price bars instead (see `reaction`).

If the calendar cannot be fetched (or is older than STALE_HOURS), a conservative time-window fallback blocks
the usual US release slots (08:30 and 10:00 New York time, weekdays). Nothing is invented: the fallback blocks
time windows, it does not claim an event exists.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STALE_HOURS = 36

# what moves gold: USD rates, inflation, jobs, growth, Fed communication
GOLD_KEYWORDS = (
    "FOMC", "Federal Funds Rate", "Fed Chair", "Powell", "Fed ", "Monetary Policy",
    "CPI", "PCE", "PPI", "Non-Farm", "Nonfarm", "NFP", "Unemployment Rate", "Unemployment Claims",
    "ADP", "Average Hourly Earnings", "JOLTS", "Retail Sales", "GDP", "ISM", "PMI", "Consumer Confidence",
    "UoM", "Durable Goods",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_events (
    id TEXT PRIMARY KEY,
    event_epoch INTEGER NOT NULL,   -- UTC epoch seconds
    title TEXT NOT NULL,
    country TEXT,
    impact TEXT,
    forecast TEXT,
    previous TEXT,
    gold_relevant INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS news_events_time ON news_events(event_epoch);
"""


def ensure(journal) -> None:
    journal.db.executescript(SCHEMA)


def relevant(e: dict[str, Any]) -> bool:
    if e.get("country") != "USD":
        return False
    if e.get("impact") == "High":
        return True
    return e.get("impact") == "Medium" and any(k.lower() in (e.get("title") or "").lower() for k in GOLD_KEYWORDS)


def fetch(url: str = FF_URL, timeout: int = 20) -> list[dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "xau-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if not isinstance(data, list):
        raise ValueError("unexpected calendar format")
    return data


def store(journal, events: list[dict[str, Any]], source: str = "forexfactory") -> int:
    ensure(journal)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for e in events:
        try:
            t = int(datetime.fromisoformat(e["date"]).timestamp())
        except (KeyError, ValueError, TypeError):
            continue
        key = hashlib.sha1(f"{e.get('country')}|{e.get('title')}|{t}".encode()).hexdigest()[:16]
        journal.db.execute(
            "INSERT INTO news_events (id, event_epoch, title, country, impact, forecast, previous, gold_relevant, "
            "source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET impact=excluded.impact, "
            "forecast=excluded.forecast, previous=excluded.previous, fetched_at=excluded.fetched_at",
            (key, t, e.get("title", ""), e.get("country"), e.get("impact"), e.get("forecast"), e.get("previous"),
             int(relevant(e)), source, now))
        n += 1
    journal.db.commit()
    return n


def refresh(journal) -> dict[str, Any]:
    """Fetch and store; logs a `news_fetch` event either way. Never raises."""
    try:
        events = fetch()
        n = store(journal, events)
        info = {"ok": True, "stored": n, "relevant": sum(1 for e in events if relevant(e))}
    except Exception as exc:  # network, format: fall back to time windows
        info = {"ok": False, "error": repr(exc)[:300]}
    journal.event("news_fetch", info)
    return info


def last_ok_fetch(journal) -> datetime | None:
    ensure(journal)
    row = journal.db.execute("SELECT MAX(fetched_at) FROM news_events").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _ny_offset_hours(d: datetime) -> int:
    """New York UTC offset without a tz database: DST from 2nd Sunday of March to 1st Sunday of November."""
    def nth_sunday(year, month, n):
        first = datetime(year, month, 1, tzinfo=timezone.utc)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    start = nth_sunday(d.year, 3, 2) + timedelta(hours=7)   # 02:00 EST = 07:00 UTC
    end = nth_sunday(d.year, 11, 1) + timedelta(hours=6)    # 02:00 EDT = 06:00 UTC
    return -4 if start <= d < end else -5


def fallback_windows(day: datetime) -> list[tuple[datetime, str]]:
    """Usual US release slots (08:30 and 10:00 New York time) on weekdays, as UTC datetimes."""
    if day.weekday() >= 5:
        return []
    off = _ny_offset_hours(day)
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return [(base + timedelta(hours=8.5 - off), "US 08:30 ET release slot (fallback)"),
            (base + timedelta(hours=10 - off), "US 10:00 ET release slot (fallback)")]


def upcoming(journal, now: datetime, hours: float = 3.0, only_relevant: bool = True) -> list[dict[str, Any]]:
    ensure(journal)
    t0 = int(now.timestamp()) - 3600
    t1 = int((now + timedelta(hours=hours)).timestamp())
    q = "SELECT * FROM news_events WHERE event_epoch BETWEEN ? AND ?" + (" AND gold_relevant = 1" if only_relevant else "")
    rows = journal.db.execute(q + " ORDER BY event_epoch", (t0, t1)).fetchall()
    return [{"time_utc": datetime.fromtimestamp(r["event_epoch"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
             "minutes_from_now": round((r["event_epoch"] - now.timestamp()) / 60),
             "title": r["title"], "impact": r["impact"], "forecast": r["forecast"], "previous": r["previous"]}
            for r in rows]


def blackout(journal, now: datetime, before_min: int = 15, after_min: int = 15) -> str | None:
    """Reason string if new entries must wait because of a high-impact USD event, else None."""
    ensure(journal)
    last = last_ok_fetch(journal)
    if last is None or now - last > timedelta(hours=STALE_HOURS):
        for t, label in fallback_windows(now):
            if t - timedelta(minutes=before_min) <= now <= t + timedelta(minutes=after_min):
                return f"news blackout: {label}; calendar unavailable or stale"
        return None
    lo = int((now - timedelta(minutes=after_min)).timestamp())
    hi = int((now + timedelta(minutes=before_min)).timestamp())
    row = journal.db.execute(
        "SELECT title, event_epoch FROM news_events WHERE country='USD' AND impact='High' "
        "AND event_epoch BETWEEN ? AND ? ORDER BY event_epoch LIMIT 1", (lo, hi)).fetchone()
    if not row:
        return None
    at = datetime.fromtimestamp(row["event_epoch"], tz=timezone.utc).strftime("%H:%M UTC")
    return f"news blackout: {row['title']} at {at} (±{before_min}/{after_min} min)"


def reaction(bars: list[dict], event_epoch: int, server_offset_sec: int, minutes: int = 30) -> dict[str, Any] | None:
    """Price reaction to an event from M1/M5 bars (server time): range and net move in the next `minutes`."""
    t0 = event_epoch + server_offset_sec
    seg = [b for b in bars if t0 <= b["time"] < t0 + minutes * 60]
    before = [b for b in bars if b["time"] < t0]
    if not seg or not before:
        return None
    ref = before[-1]["close"]
    return {"ref": ref, "high": max(b["high"] for b in seg), "low": min(b["low"] for b in seg),
            "net_points": round((seg[-1]["close"] - ref) / 0.01), "range_points": round(
                (max(b["high"] for b in seg) - min(b["low"] for b in seg)) / 0.01)}
