"""Telegram Bot API client: rate-limited (≤1 msg/s, ≤18/min), retries, honours 429 retry_after."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .http import Http, HttpError

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, http: Http, token: str, chat_id: str, sleep=asyncio.sleep, clock=time.monotonic):
        self.http, self.chat_id = http, chat_id
        self.base = f"https://api.telegram.org/bot{token}/"
        self._sleep, self._clock = sleep, clock
        self._lock = asyncio.Lock()
        self._sent: deque[float] = deque(maxlen=18)

    async def _throttle(self) -> None:
        now = self._clock()
        if self._sent and now - self._sent[-1] < 1:
            await self._sleep(1 - (now - self._sent[-1]))
        if len(self._sent) == 18 and self._clock() - self._sent[0] < 60:
            await self._sleep(60 - (self._clock() - self._sent[0]))
        self._sent.append(self._clock())

    async def _call(self, method: str, payload: dict) -> dict | None:
        async with self._lock:
            delay = 2
            for _ in range(5):
                await self._throttle()
                try:
                    _, data, _ = await self.http.post_json(self.base + method, payload, timeout=20)
                    return data.get("result")
                except HttpError as e:
                    wait = delay
                    if e.status == 429 and "retry_after" in e.body_snippet:
                        try:
                            wait = int(e.body_snippet.split('"retry_after":')[1].split("}")[0].strip())
                        except (IndexError, ValueError):
                            pass
                    elif e.status == 400:  # bad HTML etc. — retrying won't help
                        log.error("telegram %s rejected: %s", method, e.body_snippet)
                        return None
                    log.warning("telegram %s failed (%s), retry in %ss", method, e.status, wait)
                    await self._sleep(wait)
                    delay *= 2
            return None

    async def send(self, html: str) -> int | None:
        r = await self._call("sendMessage", {"chat_id": self.chat_id, "text": html,
                                             "parse_mode": "HTML", "disable_web_page_preview": True})
        return r.get("message_id") if isinstance(r, dict) else None

    async def edit(self, message_id: int, html: str) -> bool:
        r = await self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id,
                                                 "text": html, "parse_mode": "HTML",
                                                 "disable_web_page_preview": True})
        return r is not None

    async def get_chat_ids(self) -> list[tuple[str, str]]:
        _, data, _ = await self.http.post_json(self.base + "getUpdates", {}, timeout=20)
        out = {}
        for u in data.get("result", []):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
            if "id" in chat:
                out[str(chat["id"])] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
        return list(out.items())
