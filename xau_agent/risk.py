"""Deterministic risk gate. Every order passes here; the LLM cannot override it."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .broker import SymbolSpec
from .config import RiskConfig


@dataclass
class OrderRequest:
    side: str
    lot: float
    sl: float
    tp: float
    reason: str = ""
    order_type: str = "market"  # market | buy_stop | buy_limit | sell_stop | sell_limit
    price: float = 0.0  # entry price for pending orders
    expiry_minutes: int = 0


PENDING_SIDE = {"buy_stop": "buy", "buy_limit": "buy", "sell_stop": "sell", "sell_limit": "sell"}


@dataclass
class RiskContext:
    bid: float
    ask: float
    spec: SymbolSpec
    equity: float
    day_start_equity: float
    open_positions: int
    trades_today: int
    now_utc: datetime
    kill_switch: bool = False
    open_pending: int = 0
    htf_bias: str | None = None  # 'up'/'down' when M15, M30 and H1 agree
    news_block: str | None = None  # reason from the economic calendar (news.py)
    atr_m5: float | None = None  # price units


@dataclass
class RiskDecision:
    approved: bool
    lot: float
    reasons: list[str] = field(default_factory=list)
    risk_money: float = 0.0
    reward_risk: float = 0.0


def within_hours(hours, now: datetime) -> bool:
    if not hours:
        return True
    start, end = hours
    t = now.time()
    return start <= t <= end if start <= end else (t >= start or t <= end)


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(math.floor(value / step + 1e-9) * step, 8)


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check(self, req: OrderRequest, ctx: RiskContext) -> RiskDecision:
        cfg, spec = self.cfg, ctx.spec
        reasons: list[str] = []

        # ---- global gates ----
        if ctx.kill_switch:
            reasons.append("kill switch is active")
        if not within_hours(cfg.trading_hours_utc, ctx.now_utc):
            start, end = cfg.trading_hours_utc
            reasons.append(f"outside trading hours {start}-{end} UTC")
        for b_start, b_end in cfg.news_blackouts:
            if b_start <= ctx.now_utc <= b_end:
                reasons.append(f"news blackout {b_start.isoformat()} - {b_end.isoformat()}")
        if ctx.news_block:
            reasons.append(ctx.news_block)
        if ctx.day_start_equity > 0:
            loss_pct = (ctx.day_start_equity - ctx.equity) / ctx.day_start_equity * 100
            if loss_pct >= cfg.max_daily_loss_pct:
                reasons.append(f"daily loss {loss_pct:.2f}% >= limit {cfg.max_daily_loss_pct}%")
        exposure = ctx.open_positions + ctx.open_pending
        if exposure >= cfg.max_open_positions:
            reasons.append(
                f"open positions + pending orders {exposure} >= limit {cfg.max_open_positions}"
            )
        if ctx.trades_today >= cfg.max_trades_per_day:
            reasons.append(f"trades today {ctx.trades_today} >= limit {cfg.max_trades_per_day}")
        spread_pts = (ctx.ask - ctx.bid) / spec.point
        if spread_pts > cfg.max_spread_points:
            reasons.append(f"spread {spread_pts:.0f} pts > limit {cfg.max_spread_points}")

        # ---- order shape ----
        if req.side not in ("buy", "sell"):
            reasons.append(f"invalid side {req.side!r}")
            return RiskDecision(False, 0.0, reasons)
        if cfg.block_against_htf and ctx.htf_bias in ("up", "down"):
            wanted = "buy" if ctx.htf_bias == "up" else "sell"
            if req.side != wanted:
                reasons.append(f"{req.side} is against the higher-timeframe bias (M15, M30, H1 all {ctx.htf_bias})")
        if req.order_type == "market":
            entry = ctx.ask if req.side == "buy" else ctx.bid
        elif req.order_type in PENDING_SIDE:
            if PENDING_SIDE[req.order_type] != req.side:
                reasons.append(f"{req.order_type} must be side {PENDING_SIDE[req.order_type]}")
            entry = req.price
            market = ctx.ask if req.side == "buy" else ctx.bid
            above = req.order_type in ("buy_stop", "sell_limit")
            if above and not entry > market:
                reasons.append(f"{req.order_type} price {entry} must be above market {market}")
            if not above and not entry < market:
                reasons.append(f"{req.order_type} price {entry} must be below market {market}")
            dist = abs(entry - market) / spec.point
            if dist < spec.stops_level:
                reasons.append(f"pending distance {dist:.0f} pts < broker stops level {spec.stops_level}")
            if dist > cfg.max_pending_distance_points:
                reasons.append(f"pending distance {dist:.0f} pts > max {cfg.max_pending_distance_points}")
            if not 1 <= req.expiry_minutes <= cfg.max_pending_expiry_min:
                reasons.append(f"expiry_minutes must be 1..{cfg.max_pending_expiry_min}")
        else:
            reasons.append(f"invalid order_type {req.order_type!r}")
            return RiskDecision(False, 0.0, reasons)
        if req.sl <= 0 or req.tp <= 0:
            reasons.append("SL and TP are both required")
            return RiskDecision(False, 0.0, reasons)
        if req.side == "buy" and not (req.sl < entry < req.tp):
            reasons.append(f"buy needs sl < entry({entry}) < tp")
        if req.side == "sell" and not (req.tp < entry < req.sl):
            reasons.append(f"sell needs tp < entry({entry}) < sl")

        sl_pts = abs(entry - req.sl) / spec.point
        tp_pts = abs(req.tp - entry) / spec.point
        if sl_pts < max(cfg.min_sl_points, spec.stops_level):
            reasons.append(f"SL distance {sl_pts:.0f} pts < min {max(cfg.min_sl_points, spec.stops_level)}")
        if cfg.min_sl_atr and ctx.atr_m5:
            min_atr_pts = cfg.min_sl_atr * ctx.atr_m5 / spec.point
            if sl_pts < min_atr_pts:
                reasons.append(f"SL distance {sl_pts:.0f} pts < {cfg.min_sl_atr}x M5 ATR ({min_atr_pts:.0f} pts)")
        if sl_pts > cfg.max_sl_points:
            reasons.append(f"SL distance {sl_pts:.0f} pts > max {cfg.max_sl_points}")
        if tp_pts < spec.stops_level:
            reasons.append(f"TP distance {tp_pts:.0f} pts < broker stops level {spec.stops_level}")
        rr = tp_pts / sl_pts if sl_pts > 0 else 0.0
        if rr < cfg.min_reward_risk:
            reasons.append(f"reward/risk {rr:.2f} < min {cfg.min_reward_risk}")

        # ---- position size ----
        lot = min(req.lot, cfg.max_lot, spec.volume_max)
        money_per_lot = abs(entry - req.sl) / spec.tick_size * spec.tick_value if spec.tick_size > 0 else 0.0
        budget = ctx.equity * cfg.max_risk_per_trade_pct / 100
        if money_per_lot > 0 and lot * money_per_lot > budget:
            lot = budget / money_per_lot
        lot = floor_to_step(lot, spec.volume_step)
        if lot < spec.volume_min:
            reasons.append(
                f"lot after risk sizing {lot} < broker minimum {spec.volume_min} "
                f"(risk budget {budget:.2f}, risk/lot {money_per_lot:.2f})"
            )
        risk_money = round(lot * money_per_lot, 2)

        return RiskDecision(not reasons, lot if not reasons else 0.0, reasons, risk_money, round(rr, 2))
