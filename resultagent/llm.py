"""Free-tier LLM router (OpenAI-compatible APIs) with automatic failover between providers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date

from .config import LlmCfg, ModelSpecCfg
from .http import Http, HttpError
from .store import Store

log = logging.getLogger(__name__)


class LlmRouter:
    def __init__(self, http: Http, store: Store, cfg: LlmCfg):
        self.http, self.store, self.cfg = http, store, cfg
        self._minute: dict[str, list[float]] = {}
        self._cooldown: dict[str, float] = {}

    def _usable(self, m: ModelSpecCfg, vision: bool, purpose: str) -> bool:
        if vision and not m.vision:
            return False
        now = time.monotonic()
        if self._cooldown.get(m.name, 0) > now:
            return False
        recent = [t for t in self._minute.get(m.name, []) if now - t < 60]
        self._minute[m.name] = recent
        if len(recent) >= m.rpm:
            return False
        used, _ = self.store.llm_usage(date.today(), m.name)
        limit = m.rpd * (1 - self.cfg.commentary_reserve) if purpose == "commentary" else m.rpd
        return used < limit

    async def complete(self, messages: list[dict], *, vision: bool = False, purpose: str,
                       timeout: float, json_mode: bool = True, per_model: float = 12.0,
                       max_tokens: int = 1200) -> str | None:
        deadline = time.monotonic() + timeout
        for m in self.cfg.models:
            left = min(deadline - time.monotonic(), per_model)  # one slow model must not eat the budget
            if left <= 1:
                return None
            if not self._usable(m, vision, purpose):
                continue
            self._minute.setdefault(m.name, []).append(time.monotonic())
            body = {"model": m.model, "messages": messages, "temperature": 0.2 if not json_mode else 0,
                    "max_tokens": max_tokens}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            if "gpt-oss" in m.model:
                body["reasoning_effort"] = "low"  # hidden reasoning tokens otherwise eat the output budget
            try:
                _, data, _ = await asyncio.wait_for(self.http.post_json(
                    f"{m.base_url}/chat/completions", body,
                    headers={"Authorization": f"Bearer {m.api_key}"}, timeout=left), left)
            except (HttpError, asyncio.TimeoutError) as e:
                status = getattr(e, "status", 0)
                # transient (timeout / rate limit / server) -> 1 min; permanent (bad key, model gone) -> 6 h
                transient = status in (0, 429) or status >= 500
                self._cooldown[m.name] = time.monotonic() + (60 if transient else 6 * 3600)
                log.warning("llm %s failed: %s", m.name, e)
                continue
            tokens = (data.get("usage") or {}).get("total_tokens", 0)
            self.store.add_llm_usage(date.today(), m.name, 1, tokens)
            try:
                choice = data["choices"][0]
                content = choice["message"]["content"]
            except (KeyError, IndexError, TypeError):
                continue
            if not content or choice.get("finish_reason") == "length":
                log.warning("llm %s returned a cut-off/empty answer; trying next model", m.name)
                continue
            return content
        return None


def _parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
