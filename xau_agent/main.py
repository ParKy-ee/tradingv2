"""Entry point.

    python -m xau_agent.main            # run every time a DECISION_TIMEFRAME candle closes
    python -m xau_agent.main --once     # one cycle right now, then exit
    python -m xau_agent.main --check    # connect + print account/symbol info, no LLM call
    python -m xau_agent.main --label    # label past cycles with market outcomes, no LLM call
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import timedelta

from .broker import MT5Broker
from .config import load_settings
from .journal import Journal, utc_now
from . import news
from .status import keep_awake, set_stage
from .labeling import run_labeler
from .notify import Notifier
from .risk import RiskEngine
from .risk import within_hours
from .cli import make_reviewer
from .tools import ToolExecutor, sweep_pending


def has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # profile written by `ant auth login`
    return os.path.isdir(os.path.join(os.path.expanduser("~"), ".config", "anthropic"))


def guard_account(settings, account) -> None:
    """Refuse to place orders on the wrong kind of account."""
    if settings.mode == "demo" and not account.is_demo:
        sys.exit(f"MODE=demo but account {account.login} is '{account.trade_mode}'. Refusing to start.")
    if settings.mode == "live" and not account.is_demo and not settings.allow_real_account:
        sys.exit("MODE=live on a real account requires ALLOW_REAL_ACCOUNT=true. Refusing to start.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--label", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    broker = MT5Broker(
        settings.symbol, settings.magic, settings.mt5_login, settings.mt5_password,
        settings.mt5_server, settings.mt5_path,
    )
    try:
        account = broker.account()
        print(f"Connected: login={account.login} type={account.trade_mode} equity={account.equity} "
              f"{account.currency} | symbol={settings.symbol} | MODE={settings.mode}")
        guard_account(settings, account)

        if args.check:
            spec, q = broker.spec(), broker.quote()
            print(f"spec={spec}")
            print(f"bid={q.bid} ask={q.ask} spread={q.spread_points(spec.point)} pts")
            return

        journal = Journal(settings.journal_db, settings.log_dir)

        def label() -> None:
            try:
                n = run_labeler(broker, journal, settings.label_horizons_min, settings.sim_max_minutes)
                if n:
                    print(f"[label] labeled {n} past cycle(s)")
            except Exception as exc:  # labeling must never stop trading loop
                journal.event("label_error", repr(exc))
                print(f"[label] error: {exc}")

        if args.label:
            label()
            return

        notifier = Notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        risk = RiskEngine(settings.risk)
        use_cc = settings.llm_backend == "claude-code"

        if use_cc:
            if not shutil.which(settings.claude_code_bin):
                sys.exit(f"ไม่พบ Claude Code CLI ('{settings.claude_code_bin}'): ติดตั้งและ login ก่อน (ดู README)")
            claude_bin = shutil.which(settings.claude_code_bin)
            print(f"LLM backend: Claude Code CLI ({claude_bin}), model={settings.claude_code_model or 'default'}")
            client = None
        else:
            if not has_anthropic_credentials():
                sys.exit("ไม่พบ ANTHROPIC_API_KEY: ใส่ใน .env ก่อน หรือใช้ LLM_BACKEND=claude-code")
            import anthropic

            client = anthropic.Anthropic()

        from .agent import run_cycle
        from .agent_cc import run_cycle_cc

        def wait_status(detail: str, state: str = "idle") -> None:
            now = utc_now()
            nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=5 - now.minute % 5)).isoformat()
            row = journal.db.execute("SELECT cycle_id, decision, summary, error FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()
            last_res = dict(row) if row else None
            if last_res:
                last_res["summary"] = (last_res["summary"] or "")[:300]
            set_stage("m5", "wait_candle", detail, None, state, {"next_at": nxt, "last_result": last_res})

        def cycle(trigger: str) -> None:
            cycle_id = utc_now().strftime("%Y%m%d%H%M%S")
            set_stage("m5", "agent", "เริ่มรอบใหม่: Sonnet เริ่มอ่านกราฟ", cycle_id)
            try:
                if use_cc:
                    summary = run_cycle_cc(
                        broker=broker, journal=journal, mode=settings.mode, cycle_id=cycle_id, trigger=trigger,
                        claude_bin=claude_bin, model=settings.claude_code_model,
                    )
                else:
                    executor = ToolExecutor(
                        broker, risk, journal, notifier, settings.mode, settings.magic,
                        settings.kill_switch_file, cycle_id, reviewer=make_reviewer(settings),
                    )
                    summary = run_cycle(
                        client, executor, journal, model=settings.claude_model, effort=settings.claude_effort,
                        fallbacks=settings.claude_fallbacks, trigger=trigger,
                        thinking_display=settings.claude_thinking_display,
                    )
                print(f"[{cycle_id}] {summary}\n")
            except Exception as exc:
                journal.event("cycle_error", repr(exc), cycle_id)
                print(f"[{cycle_id}] cycle error: {exc}")
            row = journal.db.execute("SELECT decision, error FROM cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
            dec, err = (row["decision"], row["error"]) if row else ("error", "no cycle row")
            set_stage("m5", "done", f"{dec}" + (f" | {err[:150]}" if err else ""), cycle_id, "error" if err else "ok")
            label()
            wait_status("รอแท่ง M5 ปิดถัดไป")

        if args.once:
            cycle("Manual one-off cycle.")
            return

        tf = settings.decision_timeframe
        last = broker.last_bar_time(tf)
        paused = False
        print(f"Waiting for {tf} candle closes. Create a file named '{settings.kill_switch_file}' to pause.")
        last_sweep = 0.0
        last_label = 0.0
        last_news = 0.0
        skipping = False
        print(f"keep PC awake while running: {keep_awake()}")
        wait_status("รอแท่ง M5 ปิดถัดไป")

        def sweep(cancel_all: bool = False) -> None:
            if settings.mode == "advisory":
                return
            try:
                for t in sweep_pending(broker, journal, settings.magic, cancel_all,
                                       settings.risk.max_pending_distance_points):
                    notifier.send(f"pending order {t} cancelled ({'kill switch' if cancel_all else 'expired/tp passed/too far'})")
            except Exception as exc:
                journal.event("sweep_error", repr(exc))

        while True:
            time.sleep(2)
            if os.path.exists(settings.kill_switch_file):
                if not paused:
                    wait_status("STOP file: หยุดชั่วคราว ยกเลิกคำสั่งรอแล้ว", "blocked")
                    notifier.send("Kill switch active: agent paused, pending orders cancelled.")
                    sweep(cancel_all=True)
                    paused = True
                continue
            if time.monotonic() - last_sweep > 10:
                sweep()
                last_sweep = time.monotonic()
            if paused:
                notifier.send("Kill switch removed: agent resumed.")
                wait_status("รอแท่ง M5 ปิดถัดไป")
                paused = False
            current = broker.last_bar_time(tf)
            if settings.risk.news_auto and (last_news == 0.0 or time.monotonic() - last_news > 6 * 3600):
                info = news.refresh(journal)
                print(f"[news] calendar refresh: {info}")
                last_news = time.monotonic()
            if time.monotonic() - last_label > 600:
                label()
                last_label = time.monotonic()
            if current and current != last:
                last = current
                exposure = [p for p in broker.positions() if p.magic == settings.magic] +                     [o for o in broker.pending_orders() if o.magic == settings.magic]
                if not within_hours(settings.risk.trading_hours_utc, utc_now()) and not exposure:
                    # no new orders are allowed and nothing to manage: don't spend LLM quota
                    if not skipping:
                        wait_status("นอกเวลาเทรด: หยุดเรียก Claude (ประหยัดโควตา)")
                        journal.event("cycles_paused", "outside trading hours, no open exposure")
                        print(f"[{utc_now():%H:%M}] outside trading hours: LLM cycles paused")
                        skipping = True
                    continue
                skipping = False
                cycle(f"A new {tf} candle just opened; the previous {tf} candle is closed.")
    finally:
        broker.shutdown()


if __name__ == "__main__":
    main()
