"""Send-only Telegram client. It never reads updates, so it cannot take commands away from the main bot
that shares the same Telegram bot token."""
from __future__ import annotations

import json
import urllib.request


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200
        except Exception:
            return False
