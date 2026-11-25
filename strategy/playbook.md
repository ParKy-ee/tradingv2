# Playbook v2 (2026-11-24 21:05, demo; v1 00:10)

Status: hypotheses to test, not proven. Edited by the dev operator; every change is logged in strategy/CHANGELOG.md.

## Costs to respect
- Spread on GOLD (XM demo) is ~55 points ($0.55) and market fills slipped ~10 points in testing.
  A target smaller than ~3x spread (≈170 points) is not worth taking.
- M5 ATR14 was ~760 points. Stops tighter than ~0.6x M5 ATR get hit by noise.

## News
- `get_account_state` lists `upcoming_news_3h` (USD events that move gold) and `news_blackout_now`.
  The risk engine blocks new entries 15 min before to 15 min after high-impact USD events.
- Before a high-impact event, prefer to cancel pending orders whose expiry reaches into the event window,
  and do not open new exposure in the last ~30 min before it. Medium-impact events (e.g. Consumer Confidence,
  PMI) can still produce 2-3x ATR candles: mention them and keep stops beyond the M5 noise.

## Context first
0. Read `mtf_alignment` in the snapshot (trend per M1/M5/M15/M30/H1 from EMA20 vs EMA50 in ATR units).
   State it in your summary. When M15, M30 and H1 agree, the risk engine blocks entries against them;
   when only some agree, prefer trades in the direction of the majority of M15/M30/H1.
1. Classify the session from M15/H1: TREND (EMA20 > EMA50 and price above both, or the mirror for down)
   or RANGE (price oscillating between a clear high and low of the last 2-3 hours).
2. Mark the range high/low or the most recent swing high/low on M5.

## Entries (prefer pending orders)
- RANGE: do not trade the middle third. Fade the edges only with a limit order slightly inside the edge,
  SL beyond the edge by ~0.5x M5 ATR, TP at the middle or opposite edge (reward/risk >= 1.2).
  RANGE_FADE filters (v2, added after 4 straight RANGE_FADE losses; every losing buy faded the low
  while M5/M15/H1 EMA20 were below EMA50):
  * Do not fade an edge against the M15 bias. If M15 EMA20 < EMA50, only fade the range HIGH (sell);
    if M15 EMA20 > EMA50, only fade the range LOW (buy). If the M15 EMAs are within ~0.1x M15 ATR of
    each other, both edges are allowed.
  * Require a closed M5 rejection candle at the edge first (wick touching or through the edge, close back
    inside the range). Without it, wait; do not pre-place the limit.
  * If the edge has been tested 3+ times in the last 2 hours, expect a break: skip the fade and
    consider BREAKOUT instead.
- BREAKOUT: when price is coiling near a range edge in the direction of the H1 trend, place a stop
  order 1-2x spread beyond the edge, SL back inside the range (~0.8-1.0x M5 ATR), TP >= 1.5R,
  expiry 30-45 minutes.
- PULLBACK in TREND: limit order at the M5 EMA20 / prior breakout level, SL beyond the last swing,
  TP at the recent extreme or 1.5R, expiry 30 minutes.
- Never chase: if price already moved > 0.7x M5 ATR past your trigger, skip or wait for a pullback.

## Management
- One exposure at a time (position or pending). Cancel a pending order if the context flips
  (e.g. RANGE breaks the other way, or an M5 close invalidates the setup).
- Close an open position early only on a clear invalidation, not on small noise.

## What to log in your summary
- Context (TREND/RANGE), the levels you used, the setup name (RANGE_FADE / BREAKOUT / PULLBACK / NONE),
  and why. These names are used to measure which setups work.
