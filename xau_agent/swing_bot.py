"""H4 Donchian 20/10 swing bot: forward test of the backtested system on the DEMO account.

Rules are exactly the backtest's (xau_agent/swing.py, rule DON20_10) so live results can be compared with it:
- at each completed H4 bar: exit a long if close < lowest low of the prior 10 bars (short: > highest high);
- then, if flat: buy if close > highest high of the prior 20 bars, sell if close < lowest low of the prior 20;
- initial stop 2x ATR20 placed at the broker; no take-profit; the channel exit trails the trade.
No LLM veto on purpose: the forward test must measure the tested system, not a different discretionary one.

Runs as its own process with its own magic number, separate from the M5 agent.

    python -m xau_agent.swing_bot --check     # connect, print the current signal, place nothing
    python -m xau_agent.swing_bot             # run (MODE=demo)
    python -m xau_agent.swing_bot --report    # closed trades from MT5 deal history
STOP file blocks new entries (exits still run; the broker-side stop always protects the position).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .broker import MT5Broker
from .config import _env, _int, load_settings
from .journal import Journal
from .main import guard_account
from .notify import Notifier
from .status import keep_awake, set_stage
from .swing import atr

ENTRY_N, EXIT_N, STOP_ATR = 20, 10, 2.0
H4 = 4 * 3600


def signal(bars: list[dict], pos_side: str | None, entry_bar_time: int | None = None) -> dict[str, Any]:
    """bars: completed H4 bars, oldest first. Returns what the backtest rule would do after the last bar."""
    out: dict[str, Any] = {"bar_time": bars[-1]["time"], "exit": False, "entry": None, "atr": None}
    if len(bars) < ENTRY_N + 2:
        return out
    i = len(bars) - 1
    out["atr"] = atr(bars, i, 20)
    if pos_side:
        # check every completed bar since entry, so a missed exit (bot offline) is still taken
        first = max(EXIT_N, i if entry_bar_time is None else
                    next((k for k in range(len(bars)) if bars[k]["time"] > entry_bar_time), i))
        for k in range(first, i + 1):
            lo = min(b["low"] for b in bars[k - EXIT_N:k])
            hi = max(b["high"] for b in bars[k - EXIT_N:k])
            c = bars[k]["close"]
            if (pos_side == "buy" and c < lo) or (pos_side == "sell" and c > hi):
                out["exit"] = True
                out["exit_bar_time"] = bars[k]["time"]
                break
    if not pos_side or out["exit"]:
        hi = max(b["high"] for b in bars[i - ENTRY_N:i])
        lo = min(b["low"] for b in bars[i - ENTRY_N:i])
        c = bars[i]["close"]
        out["entry"] = "buy" if c > hi else "sell" if c < lo else None
        out["channel"] = [lo, hi]
    return out


def size_lot(equity: float, risk_pct: float, stop_dist: float, spec, max_lot: float) -> float:
    money_per_lot = stop_dist / spec.tick_size * spec.tick_value
    budget = equity * risk_pct / 100
    lot = min(budget / money_per_lot, max_lot, spec.volume_max)
    lot = math.floor(lot / spec.volume_step + 1e-9) * spec.volume_step
    if lot < spec.volume_min:
        # allow the minimum lot only if it risks at most 2x the budget
        return spec.volume_min if spec.volume_min * money_per_lot <= 2 * budget else 0.0
    return round(lot, 8)


class SwingBot:
    def __init__(self, broker, journal: Journal, notifier: Notifier, mode: str, kill_switch_file: str,
                 risk_pct: float, max_lot: float, max_spread_points: int, max_entry_delay_min: int):
        self.b, self.j, self.n = broker, journal, notifier
        self.mode, self.kill = mode, kill_switch_file
        self.risk_pct, self.max_lot = risk_pct, max_lot
        self.max_spread, self.max_delay = max_spread_points, max_entry_delay_min
        self.done_bar = 0  # last bar whose signal was fully handled
        self.publish = False  # live stage status for the dashboard (set in main)
        self._last_pub = None
        self.shadow = None  # callable(cycle_id, symbol): shadow AI review after an entry (set in main)
        # GOLD keeps the original ids ("swing-<bar>", status "swing"); other symbols get their own namespace
        sym = getattr(broker, "symbol", "GOLD")
        self.prefix = "swing-" if sym in ("GOLD", None) else f"swing-{sym}-"
        self.component = "swing" if sym in ("GOLD", None) else f"swing_{sym.lower()}"

    def _own_position(self):
        return next((p for p in self.b.positions() if p.magic == self.b.magic), None)

    def _entry_bar_time(self, ticket: int) -> int | None:
        row = self.j.db.execute("SELECT cycle_id FROM orders WHERE ticket = ? AND cycle_id LIKE 'swing-%'",
                                (ticket,)).fetchone()
        return int(row["cycle_id"].split("-")[-1]) if row else None

    def _pub(self, stage: str, detail: str, state: str = "idle") -> None:
        if self.publish and (stage, detail, state) != self._last_pub:
            self._last_pub = (stage, detail, state)
            set_stage(self.component, stage, detail, None, state)

    def step(self) -> dict[str, Any] | None:
        sig = self._step()
        try:
            pos = self._own_position()
            if pos:
                self._pub("position", f"ถือ {pos.side} {pos.volume} @ {pos.price_open} SL {pos.sl} | "
                                      f"ออกเมื่อแท่ง H4 ปิดหลุดกรอบ 10 แท่ง | กำไร {pos.profit:.2f}", "ok")
            elif sig:
                ch = sig.get("channel") or [0, 0]
                self._pub("wait_h4", f"ไม่มีสัญญาณ | ราคาต้องปิดเหนือ {ch[1]:.2f} (buy) หรือต่ำกว่า {ch[0]:.2f} (sell)",
                          "idle")
        except Exception:
            pass
        return sig

    def _step(self) -> dict[str, Any] | None:
        rows = self.b.bars("H4", 80)
        completed = rows[:-1]  # last bar is still forming
        if len(completed) < ENTRY_N + 2:
            return None
        bar_time = completed[-1]["time"]
        pos = self._own_position()
        if bar_time == self.done_bar and not pos:
            return None
        sig = signal(completed, pos.side if pos else None, self._entry_bar_time(pos.ticket) if pos else None)
        if pos and sig["exit"]:
            self._pub("broker", f"สัญญาณออก: ปิด {pos.side} {pos.ticket}", "active")
            r = self.b.close_position(pos.ticket)
            self.j.event("swing", json.dumps({"action": "exit", "ticket": pos.ticket, "ok": r.ok,
                                              "retcode": r.retcode, "price": r.price, **sig}), f"{self.prefix}{bar_time}")
            self.n.send(f"[SWING {self.mode}] exit {pos.side} {pos.ticket} ok={r.ok} @ {r.price}")
            if not r.ok:
                return sig  # retry next poll
            pos = None
        if bar_time == self.done_bar:
            return sig
        if pos or not sig["entry"]:
            self.done_bar = bar_time
            return sig
        if os.path.exists(self.kill):
            self.j.event("swing", json.dumps({"action": "skip_kill_switch", **sig}), f"{self.prefix}{bar_time}")
            self.done_bar = bar_time
            return sig
        if self.j.db.execute("SELECT 1 FROM orders WHERE cycle_id = ?", (f"{self.prefix}{bar_time}",)).fetchone():
            self.done_bar = bar_time  # already handled before a restart
            return sig
        q = self.b.quote()
        delay_min = (q.server_epoch - (bar_time + H4)) / 60 if q.server_epoch else 0
        if delay_min > self.max_delay:
            self.j.event("swing", json.dumps({"action": "skip_stale", "delay_min": delay_min, **sig}),
                         f"{self.prefix}{bar_time}")
            self.done_bar = bar_time
            return sig
        spec = self.b.spec()
        spread_pts = (q.ask - q.bid) / spec.point
        if spread_pts > self.max_spread:
            return sig  # wait for a normal spread, retry next poll within the delay window
        side = sig["entry"]
        entry = q.ask if side == "buy" else q.bid
        dist = STOP_ATR * sig["atr"]
        sl = round(entry - dist if side == "buy" else entry + dist, spec.digits)
        acc = self.b.account()
        lot = size_lot(acc.equity, self.risk_pct, dist, spec, self.max_lot)
        row = dict(cycle_id=f"{self.prefix}{bar_time}", mode=self.mode, side=side, requested_lot=lot, lot=lot, sl=sl, tp=0.0,
                   agent_reason="H4 DON20_10 entry", entry_ref=entry, spread_points=spread_pts, order_type="market")
        if lot <= 0:
            self.j.order(**row, approved=0, sent=0, ok=0, reasons="lot below broker minimum for the risk budget")
            self.done_bar = bar_time
            return sig
        if self.mode == "advisory":
            self.j.order(**row, approved=1, sent=0, ok=0, reasons="advisory")
            self.n.send(f"[SWING advisory] {side} {lot} SL {sl}")
            self.done_bar = bar_time
            return sig
        self._pub("broker", f"สัญญาณเข้า {side}: ส่งคำสั่ง {lot} lot SL {sl}", "active")
        r = self.b.send_order(side, lot, sl, 0.0, f"swing {bar_time}")
        self.j.order(**row, approved=1, sent=1, ok=int(r.ok), retcode=r.retcode, ticket=r.ticket, price=r.price)
        self.n.send(f"[SWING {self.mode}] {side} {lot} @ {r.price} SL {sl} ok={r.ok} ret={r.retcode}")
        if r.ok:
            self.done_bar = bar_time
            if self.shadow:
                self.shadow(f"{self.prefix}{bar_time}", getattr(self.b, "symbol", "GOLD"))
        return sig


def report(broker, journal: Journal) -> None:
    pat = "swing-%" if broker.symbol == "GOLD" else f"swing-{broker.symbol}-%"
    rows = [r for r in journal.db.execute("SELECT * FROM orders WHERE cycle_id LIKE ? AND ok = 1 ORDER BY ts",
                                          (pat,)).fetchall()
            if broker.symbol != "GOLD" or r["cycle_id"].split("-")[1].isdigit()]
    total_r = 0.0
    for r in rows:
        d = broker.deal_history(int(r["ticket"])) or {}
        spec = broker.spec()
        risk_money = abs((r["price"] or r["entry_ref"]) - r["sl"]) / spec.tick_size * spec.tick_value * r["lot"]
        res = d.get("net_profit")
        rr = (res / risk_money) if (res is not None and risk_money) else None
        total_r += rr or 0.0
        print(f"{r['ts'][:16]} {r['side']:4s} lot {r['lot']} entry {r['price']} SL {r['sl']} -> "
              f"{'open' if not d.get('closed') else d.get('exit_reason')} net {res} R {rr if rr is None else round(rr, 2)}")
    print(f"trades {len(rows)}, total R {total_r:+.2f} (backtest expectation ≈ +0.13R per trade)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    s = load_settings()
    magic = _int("SWING_MAGIC", s.magic + 1)
    broker = MT5Broker(s.symbol, magic, s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_path)
    acc = broker.account()
    guard_account(s, acc)
    journal = Journal(s.journal_db, s.log_dir)
    bot = SwingBot(broker, journal, Notifier(s.telegram_bot_token, s.telegram_chat_id), s.mode, s.kill_switch_file,
                   float(_env("SWING_RISK_PCT", "0.5")), float(_env("SWING_MAX_LOT", "0.05")),
                   _int("SWING_MAX_SPREAD_POINTS", 150), _int("SWING_MAX_ENTRY_DELAY_MIN", 30))
    if a.report:
        report(broker, journal)
        return
    rows = broker.bars("H4", 80)[:-1]
    pos = bot._own_position()
    sig = signal(rows, pos.side if pos else None, bot._entry_bar_time(pos.ticket) if pos else None)
    print(f"Swing bot H4 DON20_10 | login={acc.login} type={acc.trade_mode} equity={acc.equity} magic={magic} "
          f"MODE={s.mode} risk={bot.risk_pct}% max_lot={bot.max_lot}")
    print(f"last completed H4 bar {datetime.fromtimestamp(sig['bar_time'], tz=timezone.utc):%Y-%m-%d %H:%M} (server) "
          f"position={pos.side if pos else None} signal={json.dumps({k: sig[k] for k in sig if k != 'bar_time'})}")
    if a.check:
        return
    bot.publish = True
    from .shadow import launch
    bot.shadow = launch  # logged opinion only; never changes the trade
    print(f"keep PC awake while running: {keep_awake()}")
    while True:
        try:
            bot.step()
        except Exception as exc:
            journal.event("swing_error", repr(exc))
            print(f"swing error: {exc}", file=sys.stderr)
        time.sleep(20)


if __name__ == "__main__":
    main()
