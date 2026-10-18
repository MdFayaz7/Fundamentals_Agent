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
_UNSCALED = {"eps"}
_NOT_FROM_LLM = {"gnpa_pct", "nnpa_pct"}  # not cross-checked by any identity → never trust an AI read


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


_VISION_PROMPT = """You read Indian company quarterly financial results (SEBI format).
Pick the CONSOLIDATED statement for companies, STANDALONE for banks.
Return only JSON:
{"found": true, "template": "corporate|bank|lender", "statement": "standalone|consolidated",
 "unit": "crore|lakh|million|thousand|rupees",
 "columns": ["YYYY-MM-DD", "YYYY-MM-DD", "YYYY-MM-DD"],
 "rows": {"<key>": [n1, n2, n3]}}
"columns" = period-end dates of the first three QUARTER-ended columns, left to right as printed
(ignore half-year/nine-month/year columns). Each row lists those same three columns in the
same left-to-right order. Numbers as JSON numbers exactly as printed (no unit conversion),
brackets = negative, null if absent. Row keys by template:
%s
If the images contain no profit & loss results table, return {"found": false}."""


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        x = float(s.strip("()"))
    except ValueError:
        return None
    return -x if neg else x


def _column_order(cols) -> tuple[list[int], date | None]:
    """Map printed columns to [current, previous quarter, same quarter last year] by their dates."""
    try:
        ds = [date.fromisoformat(c) for c in cols][:3]
    except (TypeError, ValueError):
        return [0, 1, 2], None
    if len(ds) < 2:
        return [0, 1, 2], (ds[0] if ds else None)
    cur = max(range(len(ds)), key=lambda i: ds[i])
    def near(days):
        best = min((i for i in range(len(ds)) if i != cur),
                   key=lambda i: abs((ds[cur] - ds[i]).days - days))
        return best if abs((ds[cur] - ds[best]).days - days) <= 20 else None
    prev, yoy = near(91), near(365)
    if prev == yoy:
        prev = None
    return [cur, prev, yoy], ds[cur]


def _to_extraction(obj: dict) -> Extraction | None:
    try:
        template = Template(obj["template"])
        div = UNIT_DIVISORS[obj.get("unit") or "crore"]
        order, period = _column_order(obj.get("columns"))
        rows = {}
        for k, vals in (obj.get("rows") or {}).items():
            if k not in ROW_KEYS[template] or k in _NOT_FROM_LLM or not isinstance(vals, list):
                continue
            vals = (vals + [None, None, None])[:3]
            cells = []
            for i in order:
                v = _num(vals[i]) if i is not None else None
                cells.append(Cell(None if v is None else (v if k in _UNSCALED else v / div)))
            rows[k] = cells
        return Extraction(template=template,
                          statement="consolidated" if obj.get("statement") == "consolidated" else "standalone",
                          period_end=period, rows=rows, source="llm_vision", unit=obj.get("unit") or "crore")
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
