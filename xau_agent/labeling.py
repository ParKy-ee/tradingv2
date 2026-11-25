"""Label past cycles with what the market actually did afterwards.

For every cycle (including NO TRADE):
  forward[h] = max move up / down and close change over the next h minutes, in points (bid-based).
For every approved proposal (advisory or sent):
  sim = would SL or TP have been hit first within SIM_MAX_MINUTES, using M1 bars
        (bars are bid; sells are evaluated on ask = bid + spread at decision time;
         if SL and TP fall inside the same bar we assume SL first - conservative).
For orders actually sent:
  realized = net profit / exit reason from MT5 deal history.

Labels are only computed once the longest horizon has passed.
"""
from __future__ import annotations

from typing import Any

from .broker import Broker
from .journal import Journal


def _window(bars: list[dict[str, Any]], start_epoch: int, minutes: int) -> list[dict[str, Any]]:
    first = start_epoch // 60 * 60 + 60  # first full M1 bar after the decision
    return [b for b in bars if first <= b["time"] < first + minutes * 60]


def forward_labels(
    bars: list[dict[str, Any]], start_epoch: int, ref_bid: float, point: float, horizons: list[int]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for h in horizons:
        w = _window(bars, start_epoch, h)
        if not w:
            out[f"{h}m"] = None
            continue
        out[f"{h}m"] = {
            "up_points": round((max(b["high"] for b in w) - ref_bid) / point, 1),
            "down_points": round((ref_bid - min(b["low"] for b in w)) / point, 1),
            "close_change_points": round((w[-1]["close"] - ref_bid) / point, 1),
            "bars": len(w),
        }
    return out


def simulate_order(
    bars: list[dict[str, Any]],
    start_epoch: int,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    spread_price: float,
    point: float,
    max_minutes: int,
) -> dict[str, Any]:
    w = _window(bars, start_epoch, max_minutes)
    if not w:
        return {"outcome": "no_data"}
    mfe = mae = 0.0
    outcome, exit_price, minutes, ambiguous = "timeout", None, len(w), False
    for i, b in enumerate(w):
        if side == "buy":  # closes at bid
            hi, lo = b["high"], b["low"]
            mfe, mae = max(mfe, hi - entry), max(mae, entry - lo)
            hit_sl, hit_tp = lo <= sl, hi >= tp
        else:  # closes at ask
            hi, lo = b["high"] + spread_price, b["low"] + spread_price
            mfe, mae = max(mfe, entry - lo), max(mae, hi - entry)
            hit_sl, hit_tp = hi >= sl, lo <= tp
        if hit_sl or hit_tp:
            ambiguous = hit_sl and hit_tp
            outcome = "sl" if hit_sl else "tp"
            exit_price = sl if hit_sl else tp
            minutes = i + 1
            break
    if exit_price is None:
        last = w[-1]["close"]
        exit_price = last if side == "buy" else last + spread_price
    pnl = exit_price - entry if side == "buy" else entry - exit_price
    return {
        "outcome": outcome,
        "exit_price": round(exit_price, 5),
        "pnl_points": round(pnl / point, 1),
        "minutes_to_exit": minutes,
        "mfe_points": round(mfe / point, 1),
        "mae_points": round(mae / point, 1),
        "same_bar_sl_tp": ambiguous,
    }


def simulate_pending(
    bars: list[dict[str, Any]],
    start_epoch: int,
    order_type: str,
    price: float,
    sl: float,
    tp: float,
    spread_price: float,
    point: float,
    expiry_minutes: int,
    max_minutes: int,
) -> dict[str, Any]:
    """Pending order: find the M1 bar that triggers it (before expiry), then simulate SL/TP from there.

    buy orders fill on ask (= bid bar + spread), sell orders on bid. SL hit inside the trigger bar counts as SL.
    """
    w = _window(bars, start_epoch, expiry_minutes)
    if not w:
        return {"outcome": "no_data"}
    for i, b in enumerate(w):
        ask_hi, ask_lo = b["high"] + spread_price, b["low"] + spread_price
        hit = {
            "buy_stop": ask_hi >= price,
            "buy_limit": ask_lo <= price,
            "sell_stop": b["low"] <= price,
            "sell_limit": b["high"] >= price,
        }[order_type]
        if hit:
            side = "buy" if order_type.startswith("buy") else "sell"
            # re-run SL/TP from the trigger bar (inclusive), so start one minute before it
            res = simulate_order(bars, b["time"] - 60, side, price, sl, tp, spread_price, point, max_minutes)
            return {"triggered": True, "minutes_to_trigger": i + 1, **res}
    return {"triggered": False, "outcome": "not_triggered"}


def run_labeler(
    broker: Broker, journal: Journal, horizons: list[int], sim_max_minutes: int, bars_lookback: int = 3000
) -> int:
    """Label every cycle whose horizon has passed. Returns number of cycles labeled."""
    longest = max([*horizons, sim_max_minutes])
    now_epoch = broker.quote().server_epoch
    if not now_epoch:
        return 0
    todo = journal.cycles_to_label(now_epoch - longest * 60)
    if not todo:
        return 0
    bars = broker.bars("M1", bars_lookback)
    oldest_bar = bars[0]["time"] if bars else now_epoch
    done = 0
    for c in todo:
        if c["server_epoch"] // 60 * 60 + 60 < oldest_bar:  # lookback no longer covers this cycle
            journal.save_label(c["cycle_id"], "no_data", None, None)
            continue
        point = c["point"] or broker.spec().point
        forward = forward_labels(bars, c["server_epoch"], c["bid"], point, horizons)
        order_labels, pending = [], False
        for o in journal.orders_for_cycle(c["cycle_id"]):
            item: dict[str, Any] = {"order_id": o["id"], "side": o["side"], "approved": bool(o["approved"]),
                                    "sent": bool(o["sent"]), "ok": bool(o["ok"])}
            otype = o["order_type"] or "market"
            item["order_type"] = otype
            if o["approved"] and o["entry_ref"] is not None:
                spread_price = (o["spread_points"] or c["spread_points"] or 0) * point
                if otype == "market":
                    item["sim"] = simulate_order(
                        bars, c["server_epoch"], o["side"], o["entry_ref"], o["sl"], o["tp"],
                        spread_price, point, sim_max_minutes,
                    )
                else:
                    expiry = max(1, ((o["expires_epoch"] or c["server_epoch"]) - c["server_epoch"]) // 60)
                    item["sim"] = simulate_pending(
                        bars, c["server_epoch"], otype, o["entry_ref"], o["sl"], o["tp"],
                        spread_price, point, expiry, sim_max_minutes,
                    )
            if o["sent"] and o["ok"] and o["ticket"]:
                realized = broker.deal_history(int(o["ticket"]))
                if realized is None and otype != "market":
                    live = {p.ticket for p in broker.pending_orders()}
                    realized = {"closed": False, "triggered": False, "still_pending": o["ticket"] in live}
                    pending = pending or realized["still_pending"]
                elif not realized or not realized.get("closed"):
                    pending = True
                item["realized"] = realized
            order_labels.append(item)
        journal.save_label(c["cycle_id"], "pending_realized" if pending else "complete", forward, order_labels)
        done += 1
    return done
