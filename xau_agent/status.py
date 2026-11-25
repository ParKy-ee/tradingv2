"""Live stage status for the dashboard.

Each component (m5 / swing) writes its current stage to logs/status_<component>.json and a JS mirror next to
dashboard.html (status_<component>.js -> window.XAU_STATUS_<COMPONENT>), which the page re-loads every 2 s.
A <script> tag can load a sibling file from file://, unlike fetch(), so the dashboard stays a plain .html file.
Writing status must never break trading: every error is swallowed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FEED_MAX = 40

# stage ids in pipeline order (used by the dashboard to draw the flow)
M5_STAGES = ["wait_candle", "market", "news", "account", "agent", "risk", "opus", "broker", "done"]


def set_stage(component: str, stage: str, detail: str = "", cycle_id: str | None = None,
              state: str = "active", extra: dict[str, Any] | None = None) -> None:
    """state: active | ok | blocked | error | idle"""
    try:
        path = ROOT / "logs" / f"status_{component}.json"
        cur: dict[str, Any] = {}
        if path.exists():
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                cur = {}
        now = datetime.now(timezone.utc).isoformat()
        entry = {"ts": now, "stage": stage, "state": state, "detail": detail[:300], "cycle_id": cycle_id}
        feed = ([entry] + cur.get("feed", []))[:FEED_MAX]
        cur.update({"component": component, "stage": stage, "state": state, "detail": detail[:500],
                    "cycle_id": cycle_id or cur.get("cycle_id"), "since": now, "feed": feed, "pid": os.getpid()})
        if extra:
            cur.update(extra)
        _atomic(path, json.dumps(cur, ensure_ascii=False))
        var = f"XAU_STATUS_{component.upper()}"
        _atomic(ROOT / f"status_{component}.js", f"window.{var}={json.dumps(cur, ensure_ascii=False)};")
    except Exception:
        pass


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def keep_awake() -> bool:
    """Ask Windows not to idle-sleep while this process runs (like a media player does). Changes no settings;
    released automatically when the process exits. Returns False on non-Windows or failure."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:
        return False
