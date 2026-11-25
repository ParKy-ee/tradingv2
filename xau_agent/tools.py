"""Tool definitions given to Claude, and their executor.

Claude never talks to MT5 directly: propose_order always goes through RiskEngine,
and the mode decides whether anything is actually sent.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from . import mtf, news
from .status import set_stage
from .broker import Broker, build_snapshot
from .journal import Journal, utc_now
from .notify import Notifier
from .risk import PENDING_SIDE, OrderRequest, RiskContext, RiskEngine

_BEFORE = {
    "get_market_snapshot": ("market", "ดึงราคาและคำนวณ indicator 5 timeframe"),
    "get_account_state": ("account", "ตรวจบัญชี ไม้เปิด คำสั่งรอ และข่าว"),
    "propose_order": ("risk", "risk engine ตรวจคำสั่ง market"),
    "propose_pending_order": ("risk", "risk engine ตรวจคำสั่งรอ"),
    "cancel_pending_order": ("broker", "ยกเลิกคำสั่งรอ"),
    "close_position": ("broker", "ปิดไม้"),
}
SNAPSHOT_TIMEFRAMES = {"M1": 30, "M5": 40, "M15": 24, "M30": 24, "H1": 24}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_market_snapshot",
        "description": (
            "Current bid/ask/spread plus recent OHLC bars and indicators (EMA20, EMA50, ATR14, RSI14) "
            "for M1, M5, M15 and H1. Call this first every cycle."
        ),
        "strict": True,
        "input_schema": _obj({}),
    },
    {
        "name": "get_account_state",
        "description": (
            "Account equity, today's P&L vs start-of-day equity, open positions of this symbol, "
            "trades used today, and the hard risk limits enforced by the risk engine."
        ),
        "strict": True,
        "input_schema": _obj({}),
    },
    {
        "name": "propose_order",
        "description": (
            "Propose a market order. It is checked by a deterministic risk engine which may reject it or "
            "reduce the lot. SL and TP are absolute prices and are mandatory. At most one proposal per cycle."
        ),
        "strict": True,
        "input_schema": _obj(
            {
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "lot": {"type": "number", "description": "Requested volume in lots; may be reduced."},
                "sl": {"type": "number", "description": "Stop-loss price."},
                "tp": {"type": "number", "description": "Take-profit price."},
                "reason": {"type": "string", "description": "Short rationale referencing the data."},
            }
        ),
    },
    {
        "name": "propose_pending_order",
        "description": (
            "Place a pending order that MT5 fills automatically when price reaches `price` "
            "(buy_stop/sell_stop for breakouts, buy_limit/sell_limit for pullbacks). Prefer this when your "
            "entry trigger is a price level not reached yet. SL/TP are absolute prices measured from `price`. "
            "Cancelled automatically after expiry_minutes. Counts as this cycle's one proposal and as an "
            "open exposure for the position limit."
        ),
        "strict": True,
        "input_schema": _obj(
            {
                "order_type": {"type": "string", "enum": ["buy_stop", "buy_limit", "sell_stop", "sell_limit"]},
                "price": {"type": "number", "description": "Entry price that triggers the order."},
                "lot": {"type": "number"},
                "sl": {"type": "number"},
                "tp": {"type": "number"},
                "expiry_minutes": {"type": "integer", "description": "Auto-cancel after this many minutes."},
                "reason": {"type": "string"},
            }
        ),
    },
    {
        "name": "cancel_pending_order",
        "description": "Cancel one of this agent's pending orders when its setup is no longer valid.",
        "strict": True,
        "input_schema": _obj({"ticket": {"type": "integer"}, "reason": {"type": "string"}}),
    },
    {
        "name": "close_position",
        "description": "Close an open position opened by this agent, by ticket.",
        "strict": True,
        "input_schema": _obj(
            {
                "ticket": {"type": "integer"},
                "reason": {"type": "string"},
            }
        ),
    },
]


class ToolExecutor:
    def __init__(
        self,
        broker: Broker,
        risk: RiskEngine,
        journal: Journal,
        notifier: Notifier,
        mode: str,
        magic: int,
        kill_switch_file: str,
        cycle_id: str,
        reviewer=None,  # optional callable: second opinion before sending (see reviewer.py)
    ):
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.notifier = notifier
        self.mode = mode
        self.magic = magic
        self.kill_switch_file = kill_switch_file
        self.cycle_id = cycle_id
        self.reviewer = reviewer
        self.status_component: str | None = None  # set by the CLI to publish live stages
        self.proposals = 0
        self.actions: list[dict[str, Any]] = []  # what the agent did this cycle (for logs)

    def decision(self) -> str:
        """Compact label of the cycle outcome: buy / sell / close / rejected_buy / no_trade ..."""
        labels = []
        for a in self.actions:
            if a["tool"] == "propose_order":
                ok = a["status"] in ("sent", "advisory_only_not_sent")
                labels.append(a["side"] if ok else f"{a['status']}_{a['side']}")
            elif a["tool"] == "propose_pending_order":
                ok = a["status"] in ("placed", "advisory_only_not_sent")
                labels.append(a["order_type"] if ok else f"{a['status']}_{a['order_type']}")
            elif a["tool"] == "cancel_pending_order":
                labels.append("cancel_pending")
            elif a["tool"] == "close_position":
                labels.append("close")
        return "+".join(labels) or "no_trade"

    def kill_switch(self) -> bool:
        return bool(self.kill_switch_file) and os.path.exists(self.kill_switch_file)

    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Returns (json_result, is_error)."""
        try:
            handler = getattr(self, f"_t_{name}", None)
            if handler is None:
                return json.dumps({"error": f"unknown tool {name}"}), True
            self._stage(*_BEFORE.get(name, ("agent", name)))
            result = handler(**args)
            self._after(name, result)
            if name in ("propose_order", "close_position", "propose_pending_order", "cancel_pending_order"):
                self.actions.append({"tool": name, **args, "status": result.get("status")})
            self.journal.event(f"tool:{name}", {"args": args, "result": result}, self.cycle_id)
            return json.dumps(result, ensure_ascii=False, default=str), False
        except Exception as exc:
            self.journal.event(f"tool_error:{name}", {"args": args, "error": repr(exc)}, self.cycle_id)
            return json.dumps({"error": repr(exc)}), True

    def _stage(self, stage: str, detail: str = "", state: str = "active", **extra) -> None:
        if self.status_component:
            set_stage(self.status_component, stage, detail, self.cycle_id, state, extra or None)

    def _after(self, name: str, result: dict[str, Any]) -> None:
        if not self.status_component:
            return
        st = result.get("status")
        if name == "get_market_snapshot":
            mtf = (result.get("mtf_alignment") or {}).get("line", "")
            self._stage("agent", f"Sonnet กำลังวิเคราะห์ | {mtf}")
        elif name == "get_account_state":
            nb = result.get("news_blackout_now")
            if nb:
                self._stage("news", nb, "blocked")
            self._stage("agent", "Sonnet กำลังตัดสินใจ")
        elif st == "rejected_by_risk_engine":
            self._stage("risk", "; ".join(result.get("reasons") or [])[:300], "blocked")
        elif st == "rejected_by_reviewer":
            self._stage("opus", "; ".join(result.get("reasons") or [])[:300], "blocked")
        elif st in ("sent", "placed", "closed", "cancelled"):
            self._stage("broker", f"{name}: {st} ticket {result.get('ticket', '')}", "ok")
        elif st in ("broker_rejected",):
            self._stage("broker", f"broker rejected: {result.get('comment')}", "error")
        elif st == "rejected":
            self._stage("risk", "; ".join(result.get("reasons") or [])[:300], "blocked")

    # ---------- tools ----------
    def _t_get_market_snapshot(self) -> dict[str, Any]:
        return build_snapshot(self.broker, SNAPSHOT_TIMEFRAMES)

    def _own_positions(self):
        return [p for p in self.broker.positions() if p.magic == self.magic]

    def _own_pending(self):
        return [o for o in self.broker.pending_orders() if o.magic == self.magic]

    def _risk_ctx(self, q) -> RiskContext:
        acc = self.broker.account()
        return RiskContext(
            bid=q.bid,
            ask=q.ask,
            spec=self.broker.spec(),
            equity=acc.equity,
            day_start_equity=self.journal.day_start_equity(acc.equity),
            open_positions=len(self._own_positions()),
            open_pending=len(self._own_pending()),
            trades_today=self._trades_today(),
            now_utc=utc_now(),
            kill_switch=self.kill_switch(),
            htf_bias=self._htf_bias(),
            news_block=self._news_block(),
            atr_m5=self._atr_m5(),
        )

    def _review(self, row: dict[str, Any], proposal: dict[str, Any], reason: str) -> dict[str, Any] | None:
        """Ask the reviewer model; return a rejection result to hand back to the agent, or None to proceed."""
        if not self.reviewer:
            return None
        from .agent import load_playbook
        from .stats import stats_text
        try:
            snapshot = build_snapshot(self.broker, SNAPSHOT_TIMEFRAMES)
            snapshot["upcoming_news_3h"] = self._upcoming_news()
        except Exception as exc:
            snapshot = {"error": repr(exc)}
        self._stage("opus", f"Opus กำลังตรวจทาน {proposal.get('order_type')} {proposal.get('side')}")
        v = self.reviewer(proposal, reason, snapshot, stats_text(self.journal, self.broker), load_playbook())
        self.journal.event("order_review", json.dumps({**v, "proposal": proposal}, default=str), self.cycle_id)
        if v.get("verdict") == "approve":
            return None
        why = f"reviewer({v.get('model')}): {v.get('reason')}"
        self.journal.order(**{**row, "approved": 0, "reasons": why}, sent=0, ok=0)
        self.notifier.send(f"[{self.mode.upper()}] order rejected by reviewer | {why}")
        return {"status": "rejected_by_reviewer", "reasons": [why],
                "note": "Accept this decision; do not reshape the same idea to get it through."}

    def _trades_today(self) -> int:
        """Trades that actually used risk today: market orders, plus pending orders that filled or are still
        working. Expired/cancelled pendings and operator test orders (cycle_id 'manual%') do not count."""
        from .journal import utc_day
        rows = self.journal.db.execute(
            "SELECT order_type, ticket, sent, ok FROM orders WHERE day = ? AND mode = ? AND approved = 1 "
            "AND cycle_id NOT LIKE 'manual%' AND cycle_id NOT LIKE 'swing-%'", (utc_day(), self.mode)).fetchall()
        working = {o.ticket for o in self._own_pending()}
        n = 0
        for r in rows:
            if not r["sent"]:
                n += 1  # advisory: count would-be orders
            elif not r["ok"]:
                continue
            elif (r["order_type"] or "market") == "market":
                n += 1
            elif r["ticket"] in working:
                n += 1
            else:
                try:
                    n += 1 if self.broker.deal_history(int(r["ticket"])) else 0
                except Exception:
                    n += 1  # unknown: be conservative
        return n

    def _atr_m5(self):
        try:
            from . import indicators
            rows = self.broker.bars("M5", 200)
            return indicators.atr([r["high"] for r in rows], [r["low"] for r in rows], [r["close"] for r in rows], 14)
        except Exception:
            return None

    def _news_block(self):
        cfg = self.risk.cfg
        if not cfg.news_auto:
            return None
        try:
            return news.blackout(self.journal, utc_now(), cfg.news_before_min, cfg.news_after_min)
        except Exception as exc:
            self.journal.event("news_error", repr(exc), self.cycle_id)
            return None

    def _upcoming_news(self):
        try:
            return news.upcoming(self.journal, utc_now(), hours=3)
        except Exception as exc:
            return [{"error": repr(exc)}]

    def _htf_bias(self):
        try:
            return mtf.htf_bias(self.broker)
        except Exception as exc:  # missing bars must not block trading silently; log and allow
            self.journal.event("mtf_error", repr(exc), self.cycle_id)
            return None

    def _one_per_cycle(self) -> bool:
        # counted in the journal too, because the claude-code backend runs each tool in a new process
        if self.proposals >= 1 or self.journal.orders_for_cycle(self.cycle_id):
            return False
        self.proposals += 1
        return True

    def _t_get_account_state(self) -> dict[str, Any]:
        acc = self.broker.account()
        day_start = self.journal.day_start_equity(acc.equity)
        cfg = self.risk.cfg
        return {
            "mode": self.mode,
            "account_type": acc.trade_mode,
            "currency": acc.currency,
            "balance": acc.balance,
            "equity": acc.equity,
            "day_start_equity": day_start,
            "day_pnl": round(acc.equity - day_start, 2),
            "open_positions": [asdict(p) for p in self._own_positions()],
            "pending_orders": [
                {**asdict(o), "expires_server_epoch": self.journal.pending_expiry(o.ticket)}
                for o in self._own_pending()
            ],
            "trades_today": self._trades_today(),
            "kill_switch": self.kill_switch(),
            "upcoming_news_3h": self._upcoming_news(),
            "news_blackout_now": self._news_block(),
            "limits": {
                "max_lot": cfg.max_lot,
                "max_risk_per_trade_pct": cfg.max_risk_per_trade_pct,
                "max_daily_loss_pct": cfg.max_daily_loss_pct,
                "max_open_positions": cfg.max_open_positions,
                "max_trades_per_day": cfg.max_trades_per_day,
                "max_spread_points": cfg.max_spread_points,
                "sl_points_range": [cfg.min_sl_points, cfg.max_sl_points],
                "min_sl_atr_m5": cfg.min_sl_atr,
                "min_reward_risk": cfg.min_reward_risk,
                "max_pending_distance_points": cfg.max_pending_distance_points,
                "max_pending_expiry_min": cfg.max_pending_expiry_min,
            },
        }

    def _t_propose_order(self, side: str, lot: float, sl: float, tp: float, reason: str) -> dict[str, Any]:
        if not self._one_per_cycle():
            return {"status": "rejected", "reasons": ["only one proposal per cycle"]}
        q = self.broker.quote()
        ctx = self._risk_ctx(q)
        req = OrderRequest(side=side, lot=lot, sl=sl, tp=tp, reason=reason)
        decision = self.risk.check(req, ctx)
        row = dict(
            cycle_id=self.cycle_id, mode=self.mode, side=side, requested_lot=lot, lot=decision.lot,
            sl=sl, tp=tp, approved=int(decision.approved), reasons="; ".join(decision.reasons),
            agent_reason=reason,
            entry_ref=q.ask if side == "buy" else q.bid,
            spread_points=q.spread_points(ctx.spec.point),
            order_type="market",
        )

        if not decision.approved:
            self.journal.order(**row, sent=0, ok=0)
            return {"status": "rejected_by_risk_engine", "reasons": decision.reasons}

        if self.mode == "advisory":
            check = self.broker.check_order(side, decision.lot, sl, tp)
            self.journal.order(**row, sent=0, ok=int(check.ok), retcode=check.retcode)
            self.notifier.send(f"[ADVISORY] {side} {decision.lot} SL {sl} TP {tp} | {reason}")
            return {
                "status": "advisory_only_not_sent",
                "lot": decision.lot,
                "risk_money": decision.risk_money,
                "reward_risk": decision.reward_risk,
                "broker_order_check": {"ok": check.ok, "retcode": check.retcode, "comment": check.comment},
            }

        blocked = self._review(row, {"order_type": "market", "side": side, "lot": decision.lot,
                                     "entry_ref": row["entry_ref"], "sl": sl, "tp": tp,
                                     "reward_risk": decision.reward_risk, "risk_money": decision.risk_money}, reason)
        if blocked:
            return blocked
        self._stage("broker", f"ส่งคำสั่ง {side} {decision.lot} ไป MT5")
        result = self.broker.send_order(side, decision.lot, sl, tp, f"xau-agent {self.cycle_id}")
        self.journal.order(
            **row, sent=1, ok=int(result.ok), retcode=result.retcode, ticket=result.ticket, price=result.price
        )
        self.notifier.send(
            f"[{self.mode.upper()}] {side} {decision.lot} @ {result.price} SL {sl} TP {tp} "
            f"ok={result.ok} ret={result.retcode} | {reason}"
        )
        return {
            "status": "sent" if result.ok else "broker_rejected",
            "lot": decision.lot,
            "risk_money": decision.risk_money,
            "retcode": result.retcode,
            "comment": result.comment,
            "ticket": result.ticket,
            "price": result.price,
        }

    def _t_close_position(self, ticket: int, reason: str) -> dict[str, Any]:
        if ticket not in {p.ticket for p in self._own_positions()}:
            return {"status": "rejected", "reasons": ["ticket is not an open position of this agent"]}
        if self.mode == "advisory":
            self.notifier.send(f"[ADVISORY] close {ticket} | {reason}")
            return {"status": "advisory_only_not_sent"}
        result = self.broker.close_position(ticket)
        self.notifier.send(f"[{self.mode.upper()}] close {ticket} ok={result.ok} ret={result.retcode} | {reason}")
        return {"status": "closed" if result.ok else "broker_rejected", "retcode": result.retcode,
                "comment": result.comment, "price": result.price}

    def _t_propose_pending_order(self, order_type: str, price: float, lot: float, sl: float, tp: float,
                                 expiry_minutes: int, reason: str) -> dict[str, Any]:
        if not self._one_per_cycle():
            return {"status": "rejected", "reasons": ["only one proposal per cycle"]}
        side = PENDING_SIDE.get(order_type, "?")
        q = self.broker.quote()
        ctx = self._risk_ctx(q)
        req = OrderRequest(side=side, lot=lot, sl=sl, tp=tp, reason=reason, order_type=order_type,
                           price=price, expiry_minutes=expiry_minutes)
        decision = self.risk.check(req, ctx)
        expires = q.server_epoch + int(expiry_minutes) * 60 if q.server_epoch else None
        row = dict(
            cycle_id=self.cycle_id, mode=self.mode, side=side, requested_lot=lot, lot=decision.lot,
            sl=sl, tp=tp, approved=int(decision.approved), reasons="; ".join(decision.reasons),
            agent_reason=reason, entry_ref=price, spread_points=q.spread_points(ctx.spec.point),
            order_type=order_type, expires_epoch=expires,
        )
        if not decision.approved:
            self.journal.order(**row, sent=0, ok=0)
            return {"status": "rejected_by_risk_engine", "reasons": decision.reasons}

        if self.mode == "advisory":
            check = self.broker.check_pending(order_type, decision.lot, price, sl, tp)
            self.journal.order(**row, sent=0, ok=int(check.ok), retcode=check.retcode)
            self.notifier.send(f"[ADVISORY] {order_type} {decision.lot} @ {price} SL {sl} TP {tp} | {reason}")
            return {"status": "advisory_only_not_sent", "lot": decision.lot, "risk_money": decision.risk_money,
                    "reward_risk": decision.reward_risk,
                    "broker_order_check": {"ok": check.ok, "retcode": check.retcode, "comment": check.comment}}

        blocked = self._review(row, {"order_type": order_type, "side": side, "price": price, "lot": decision.lot,
                                     "sl": sl, "tp": tp, "expiry_minutes": expiry_minutes,
                                     "reward_risk": decision.reward_risk, "risk_money": decision.risk_money}, reason)
        if blocked:
            return blocked
        self._stage("broker", f"ส่งคำสั่งรอ {order_type} @ {price} ไป MT5")
        result = self.broker.place_pending(order_type, decision.lot, price, sl, tp, f"xau {self.cycle_id}",
                                           expires_epoch=expires)
        self.journal.order(**row, sent=1, ok=int(result.ok), retcode=result.retcode, ticket=result.ticket,
                           price=price)
        self.notifier.send(f"[{self.mode.upper()}] {order_type} {decision.lot} @ {price} SL {sl} TP {tp} "
                           f"exp {expiry_minutes}m ok={result.ok} ret={result.retcode} | {reason}")
        return {"status": "placed" if result.ok else "broker_rejected", "ticket": result.ticket,
                "lot": decision.lot, "risk_money": decision.risk_money, "reward_risk": decision.reward_risk,
                "expires_in_minutes": expiry_minutes, "retcode": result.retcode, "comment": result.comment}

    def _t_cancel_pending_order(self, ticket: int, reason: str) -> dict[str, Any]:
        if ticket not in {o.ticket for o in self._own_pending()}:
            return {"status": "rejected", "reasons": ["ticket is not a pending order of this agent"]}
        if self.mode == "advisory":
            return {"status": "advisory_only_not_sent"}
        result = self.broker.cancel_pending(ticket)
        self.notifier.send(f"[{self.mode.upper()}] cancel pending {ticket} ok={result.ok} | {reason}")
        return {"status": "cancelled" if result.ok else "broker_rejected", "retcode": result.retcode,
                "comment": result.comment}


def sweep_pending(broker: Broker, journal: Journal, magic: int, cancel_all: bool = False,
                  max_distance_points: float | None = None) -> list[int]:
    """Cancel this agent's pending orders that should no longer be working (or all of them, e.g. kill switch).

    Reasons, checked every few seconds without any LLM call:
    - expired: our expiry time passed (the broker also expires it server-side);
    - tp_passed: price already reached the order's TP without filling it -> the move is gone; a fill now would
      enter after the target and with most of the reward spent;
    - too_far: price ran away from the entry by more than the distance the risk engine allows for a new order.
    """
    mine = [o for o in broker.pending_orders() if o.magic == magic]
    if not mine:
        return []
    q = broker.quote()
    now = q.server_epoch
    point = broker.spec().point
    cancelled = []
    for o in mine:
        reason = "kill_switch" if cancel_all else None
        exp = journal.pending_expiry(o.ticket)
        buy = o.order_type.startswith("buy")
        market = q.ask if buy else q.bid
        if not reason and exp is not None and now and now >= exp:
            reason = "expired"
        if not reason and o.tp and ((buy and q.bid >= o.tp) or (not buy and q.ask <= o.tp)):
            reason = "tp_passed"
        if not reason and max_distance_points and abs(o.price - market) / point > max_distance_points:
            reason = "too_far"
        if not reason:
            continue
        res = broker.cancel_pending(o.ticket)
        journal.event("pending_cancelled", {"ticket": o.ticket, "reason": reason, "price": o.price, "tp": o.tp,
                                            "bid": q.bid, "ask": q.ask, "ok": res.ok, "retcode": res.retcode})
        if res.ok:
            cancelled.append(o.ticket)
    return cancelled
