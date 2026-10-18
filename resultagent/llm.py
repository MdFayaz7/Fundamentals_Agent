"""Free-tier LLM router (OpenAI-compatible APIs) for the optional plain-English commentary."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date

from .config import LlmCfg, ModelSpecCfg
from .http import Http, HttpError
from .models import Analysis
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

    async def complete(self, messages: list[dict], *, vision: bool, purpose: str,
                       timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        for m in self.cfg.models:
            left = min(deadline - time.monotonic(), 12.0)  # one slow model must not eat the budget
            if left <= 1:
                return None
            if not self._usable(m, vision, purpose):
                continue
            self._minute.setdefault(m.name, []).append(time.monotonic())
            body = {"model": m.model, "messages": messages, "temperature": 0,
                    "response_format": {"type": "json_object"}, "max_tokens": 1200}
            try:
                _, data, _ = await asyncio.wait_for(self.http.post_json(
                    f"{m.base_url}/chat/completions", body,
                    headers={"Authorization": f"Bearer {m.api_key}"}, timeout=left), left)
            except (HttpError, asyncio.TimeoutError) as e:
                status = getattr(e, "status", 0)
                self._cooldown[m.name] = time.monotonic() + (60 if status in (0, 429) or status >= 500 else 600)
                log.warning("llm %s failed: %s", m.name, e)
                continue
            tokens = (data.get("usage") or {}).get("total_tokens", 0)
            self.store.add_llm_usage(date.today(), m.name, 1, tokens)
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                continue
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


async def llm_commentary(router: LlmRouter, symbol: str, a: Analysis, timeout: float) -> list[str] | None:
    facts = {"symbol": symbol, "verdict": a.verdict, "score": a.score,
             "metrics": [{"name": m.name, "value": m.value, "yoy": m.yoy, "qoq": m.qoq, "kind": m.kind}
                         for m in a.metrics],
             "flags": [f.text for f in a.flags]}
    prompt = ("You are a fundamentals analyst writing for a retail trader who is not an expert. "
              "From these quarterly results facts (₹ crore; yoy/qoq in %, margin in bps, pct in pp) "
              'write 3-4 short plain-English insights, each under 110 characters. '
              'Return JSON {"bullets": [...]}. Facts: ' + json.dumps(facts, default=str))
    obj = _parse_json(await router.complete([{"role": "user", "content": prompt}],
                                            vision=False, purpose="commentary", timeout=timeout))
    bullets = obj.get("bullets") if obj else None
    if not isinstance(bullets, list):
        return None
    return [str(b) for b in bullets if str(b).strip()][:4] or None
