"""Shell interface to the same tools, for the claude-code backend.

    python -m xau_agent.cli snapshot
    python -m xau_agent.cli account
    python -m xau_agent.cli propose --side buy --lot 0.01 --sl 3660.5 --tp 3672.5 --reason "..."
    python -m xau_agent.cli close --ticket 123 --reason "..."

Every call goes through ToolExecutor (risk engine + journal), exactly like the API backend.
The cycle id comes from XAU_CYCLE_ID (set by main.py); prints one JSON object.
"""
from __future__ import annotations

import argparse
import os
import sys

from .broker import MT5Broker
from .config import load_settings
from .journal import Journal, utc_now
from .notify import Notifier
from .risk import RiskEngine
from .reviewer import OrderReviewer
from .tools import ToolExecutor



def make_reviewer(s):
    """Second-opinion model on every order (ORDER_REVIEW_MODEL, e.g. opus). Empty = off."""
    if not s.order_review_model or s.mode == "advisory":
        return None
    return OrderReviewer(s.claude_code_bin, s.order_review_model, s.order_review_timeout, s.order_review_fail_open)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xau_agent.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("account")
    pr = sub.add_parser("propose")
    pr.add_argument("--side", required=True, choices=["buy", "sell"])
    pr.add_argument("--lot", required=True, type=float)
    pr.add_argument("--sl", required=True, type=float)
    pr.add_argument("--tp", required=True, type=float)
    pr.add_argument("--reason", required=True)
    pe = sub.add_parser("pending")
    pe.add_argument("--type", required=True, choices=["buy_stop", "buy_limit", "sell_stop", "sell_limit"])
    pe.add_argument("--price", required=True, type=float)
    pe.add_argument("--lot", required=True, type=float)
    pe.add_argument("--sl", required=True, type=float)
    pe.add_argument("--tp", required=True, type=float)
    pe.add_argument("--expiry", required=True, type=int)
    pe.add_argument("--reason", required=True)
    ca = sub.add_parser("cancel")
    ca.add_argument("--ticket", required=True, type=int)
    ca.add_argument("--reason", required=True)
    cl = sub.add_parser("close")
    cl.add_argument("--ticket", required=True, type=int)
    cl.add_argument("--reason", required=True)
    a = p.parse_args(argv)

    s = load_settings()
    cycle_id = os.environ.get("XAU_CYCLE_ID") or "cli-" + utc_now().strftime("%Y%m%d%H%M%S")
    broker = MT5Broker(s.symbol, s.magic, s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_path)
    journal = None
    try:
        acc = broker.account()
        if s.mode == "demo" and not acc.is_demo:
            print('{"error": "MODE=demo but account is not demo"}')
            return 2
        if s.mode == "live" and not acc.is_demo and not s.allow_real_account:
            print('{"error": "live trading on a real account is not allowed"}')
            return 2
        journal = Journal(s.journal_db, s.log_dir)
        ex = ToolExecutor(broker, RiskEngine(s.risk), journal, Notifier(
            s.telegram_bot_token, s.telegram_chat_id), s.mode, s.magic, s.kill_switch_file, cycle_id,
            reviewer=make_reviewer(s))
        ex.status_component = "m5" if os.environ.get("XAU_CYCLE_ID", "").isdigit() else None
        if a.cmd == "snapshot":
            out, err = ex.execute("get_market_snapshot", {})
        elif a.cmd == "account":
            out, err = ex.execute("get_account_state", {})
        elif a.cmd == "propose":
            out, err = ex.execute("propose_order", {"side": a.side, "lot": a.lot, "sl": a.sl, "tp": a.tp,
                                                    "reason": a.reason})
        elif a.cmd == "pending":
            out, err = ex.execute("propose_pending_order", {
                "order_type": a.type, "price": a.price, "lot": a.lot, "sl": a.sl, "tp": a.tp,
                "expiry_minutes": a.expiry, "reason": a.reason})
        elif a.cmd == "cancel":
            out, err = ex.execute("cancel_pending_order", {"ticket": a.ticket, "reason": a.reason})
        else:
            out, err = ex.execute("close_position", {"ticket": a.ticket, "reason": a.reason})
        print(out)
        return 1 if err else 0
    finally:
        if journal:
            journal.close()
        broker.shutdown()


if __name__ == "__main__":
    sys.exit(main())
