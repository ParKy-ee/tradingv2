"""Optional Telegram alerts. Silently disabled when not configured."""
from __future__ import annotations

import json
import sys
import urllib.request


class Notifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        print(f"[notify] {text}", file=sys.stderr)  # stderr: keeps CLI JSON output clean
        if not (self.bot_token and self.chat_id):
            return
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:  # alerts must never crash trading loop
            print(f"[notify] telegram failed: {exc}")
