"""Free-tier LLM router (OpenAI-compatible APIs) + the two LLM jobs: vision extraction, commentary."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import date

from .config import LlmCfg, ModelSpecCfg
from .extract import ROW_KEYS
from .http import Http, HttpError
from .models import Analysis, Cell, Extraction, Template
from .numparse import UNIT_DIVISORS
from .store import Store

log = logging.getLogger(__name__)
NOT_FOUND = object()  # vision saw no P&L table
_UNSCALED = {"eps", "gnpa_pct", "nnpa_pct"}


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
            left = deadline - time.monotonic()
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


_VISION_PROMPT = """You read Indian company quarterly financial results (SEBI format).
Pick the CONSOLIDATED statement for companies, STANDALONE for banks.
Return only JSON:
{"found": true, "template": "corporate|bank|lender", "statement": "standalone|consolidated",
 "unit": "crore|lakh|million|thousand|rupees", "period_end": "YYYY-MM-DD",
 "rows": {"<key>": [current_quarter, previous_quarter, same_quarter_last_year]}}
Use only QUARTER columns (ignore half-year/nine-month/year). Values exactly as printed,
brackets = negative, null if absent. Row keys by template:
%s
If the images contain no profit & loss results table, return {"found": false}."""


def _to_extraction(obj: dict) -> Extraction | None:
    try:
        template = Template(obj["template"])
        div = UNIT_DIVISORS[obj.get("unit") or "crore"]
        rows = {}
        for k, vals in (obj.get("rows") or {}).items():
            if k not in ROW_KEYS[template] or not isinstance(vals, list):
                continue
            cells = []
            for v in (vals + [None, None, None])[:3]:
                v = float(v) if isinstance(v, (int, float)) else None
                cells.append(Cell(None if v is None else (v if k in _UNSCALED else v / div)))
            rows[k] = cells
        pe = obj.get("period_end")
        return Extraction(template=template,
                          statement="consolidated" if obj.get("statement") == "consolidated" else "standalone",
                          period_end=date.fromisoformat(pe) if pe else None,
                          rows=rows, source="llm_vision", unit=obj.get("unit") or "crore")
    except (KeyError, ValueError, TypeError):
        return None


async def llm_extract_vision(router: LlmRouter, jpegs: list[bytes], timeout: float):
    keys = "\n".join(f"{t.value}: {', '.join(k)}" for t, k in ROW_KEYS.items())
    content = [{"type": "text", "text": _VISION_PROMPT % keys}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(j).decode()}}
        for j in jpegs]
    obj = _parse_json(await router.complete([{"role": "user", "content": content}],
                                            vision=True, purpose="extract", timeout=timeout))
    if obj is None:
        return None
    if obj.get("found") is False:
        return NOT_FOUND
    return _to_extraction(obj)


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
