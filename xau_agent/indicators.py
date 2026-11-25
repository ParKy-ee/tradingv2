"""Small dependency-free indicators. All return the value for the last bar."""
from __future__ import annotations


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    for v in values[period:]:
        current = v * k + current * (1 - k)
    return current


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    # Wilder smoothing
    current = sum(trs[:period]) / period
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def trend(highs: list[float], lows: list[float], closes: list[float], flat_gap_atr: float = 0.1) -> dict | None:
    """up / down / flat from EMA20 vs EMA50 (gap measured in ATR14) and price vs EMA50."""
    e20, e50, a = ema(closes, 20), ema(closes, 50), atr(highs, lows, closes, 14)
    if e20 is None or e50 is None or not a:
        return None
    gap = (e20 - e50) / a
    c = closes[-1]
    if gap > flat_gap_atr and c > e50:
        t = "up"
    elif gap < -flat_gap_atr and c < e50:
        t = "down"
    else:
        t = "flat"
    return {"trend": t, "ema_gap_atr": round(gap, 2)}
