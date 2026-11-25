"""Settings loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

VALID_MODES = ("advisory", "demo", "live")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def parse_hours(raw: str) -> tuple[time, time] | None:
    if not raw:
        return None
    start, end = raw.split("-")
    return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())


def parse_blackouts(raw: str) -> list[tuple[datetime, datetime]]:
    """'2026-12-04T13:30Z/45,...' -> [(start, end)] in UTC. Window is +/- minutes."""
    windows: list[tuple[datetime, datetime]] = []
    for item in filter(None, (p.strip() for p in raw.split(","))):
        stamp, minutes = item.split("/")
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        delta = timedelta(minutes=int(minutes))
        windows.append((at - delta, at + delta))
    return windows


@dataclass
class RiskConfig:
    symbol: str = "GOLD"
    max_lot: float = 0.01
    max_risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_open_positions: int = 1
    max_trades_per_day: int = 10
    max_spread_points: float = 40
    min_sl_points: float = 50
    max_sl_points: float = 600
    min_reward_risk: float = 1.0
    max_pending_distance_points: float = 1500
    max_pending_expiry_min: int = 60
    block_against_htf: bool = True
    news_auto: bool = True
    min_sl_atr: float = 0.6  # playbook: stops tighter than ~0.6x M5 ATR get hit by noise
    news_before_min: int = 15
    news_after_min: int = 15
    trading_hours_utc: tuple[time, time] | None = None
    news_blackouts: list[tuple[datetime, datetime]] = field(default_factory=list)


@dataclass
class Settings:
    mt5_login: int | None
    mt5_password: str
    mt5_server: str
    mt5_path: str
    symbol: str
    magic: int
    mode: str
    allow_real_account: bool
    decision_timeframe: str
    risk: RiskConfig
    kill_switch_file: str
    claude_model: str
    claude_effort: str
    claude_fallbacks: str
    claude_thinking_display: str
    llm_backend: str
    claude_code_bin: str
    claude_code_model: str
    order_review_model: str
    order_review_timeout: int
    order_review_fail_open: bool
    journal_db: str
    log_dir: str
    label_horizons_min: list[int]
    sim_max_minutes: int
    telegram_bot_token: str
    telegram_chat_id: str


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    mode = _env("MODE", "advisory").lower()
    if mode not in VALID_MODES:
        raise ValueError(f"MODE must be one of {VALID_MODES}, got {mode!r}")

    symbol = _env("SYMBOL", "GOLD")
    login = _env("MT5_LOGIN")
    risk = RiskConfig(
        symbol=symbol,
        max_lot=_float("MAX_LOT", 0.01),
        max_risk_per_trade_pct=_float("MAX_RISK_PER_TRADE_PCT", 0.5),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 2.0),
        max_open_positions=_int("MAX_OPEN_POSITIONS", 1),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 10),
        max_spread_points=_float("MAX_SPREAD_POINTS", 40),
        min_sl_points=_float("MIN_SL_POINTS", 50),
        max_sl_points=_float("MAX_SL_POINTS", 600),
        min_reward_risk=_float("MIN_REWARD_RISK", 1.0),
        max_pending_distance_points=_float("MAX_PENDING_DISTANCE_POINTS", 1500),
        max_pending_expiry_min=_int("MAX_PENDING_EXPIRY_MIN", 60),
        block_against_htf=_bool("BLOCK_AGAINST_HTF", True),
        news_auto=_bool("NEWS_AUTO", True),
        min_sl_atr=float(_env("MIN_SL_ATR", "0.6")),
        news_before_min=_int("NEWS_BLOCK_BEFORE_MIN", 15),
        news_after_min=_int("NEWS_BLOCK_AFTER_MIN", 15),
        trading_hours_utc=parse_hours(_env("TRADING_HOURS_UTC")),
        news_blackouts=parse_blackouts(_env("NEWS_BLACKOUTS")),
    )
    return Settings(
        mt5_login=int(login) if login else None,
        mt5_password=_env("MT5_PASSWORD"),
        mt5_server=_env("MT5_SERVER"),
        mt5_path=_env("MT5_PATH"),
        symbol=symbol,
        magic=_int("MAGIC", 260512),
        mode=mode,
        allow_real_account=_bool("ALLOW_REAL_ACCOUNT"),
        decision_timeframe=_env("DECISION_TIMEFRAME", "M5").upper(),
        risk=risk,
        kill_switch_file=_env("KILL_SWITCH_FILE", "STOP"),
        claude_model=_env("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=_env("CLAUDE_EFFORT", "low"),
        claude_fallbacks=_env("CLAUDE_FALLBACKS", "default").lower(),
        claude_thinking_display=_env("CLAUDE_THINKING_DISPLAY", "summarized").lower(),
        llm_backend=_env("LLM_BACKEND", "api").lower(),
        claude_code_bin=_env("CLAUDE_CODE_BIN", "claude"),
        claude_code_model=_env("CLAUDE_CODE_MODEL"),
        order_review_model=_env("ORDER_REVIEW_MODEL"),
        order_review_timeout=_int("ORDER_REVIEW_TIMEOUT", 90),
        order_review_fail_open=_bool("ORDER_REVIEW_FAIL_OPEN"),
        journal_db=_env("JOURNAL_DB", "journal.sqlite"),
        log_dir=_env("LOG_DIR", "logs"),
        label_horizons_min=[int(x) for x in _env("LABEL_HORIZONS_MIN", "15,60").split(",") if x.strip()],
        sim_max_minutes=_int("SIM_MAX_MINUTES", 240),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
    )
