"""One filing → (maybe) one Telegram flash + one event. Never raises."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from . import pdftext
from .analyse import analyse
from .config import Settings
from .extract import detect_template, extract_rows
from .fmt import append_ai_view, format_flash, format_headline
from .http import Http, HttpError, TooLarge
from .llm import NOT_FOUND, LlmRouter, llm_commentary, llm_extract_vision
from .models import Confidence, Filing, Template
from .numparse import fallback_period_end, fy_label
from .store import Store
from .telegram import Telegram
from .validate import validate

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MAX_PDF = 40 * 1024 * 1024
_RESULT_WORDS = re.compile(r"financial\s+results?|statement\s+of\s+.{0,40}results|un-?audited", re.I)
_REFERER = {"NSE": "https://www.nseindia.com/", "BSE": "https://www.bseindia.com/"}


class Pipeline:
    extract_timeout = 25.0
    commentary_timeout = 20.0
    pdf_retry_waits = (0, 3, 10)

    def __init__(self, settings: Settings, store: Store, http: Http, router: LlmRouter | None,
                 telegram: Telegram, now=lambda: datetime.now(IST)):
        self.s, self.store, self.http, self.router, self.tg, self.now = settings, store, http, router, telegram, now
        self._locks: dict[str, asyncio.Lock] = {}
        self._dl = asyncio.Semaphore(3)
        self.pending_commentary: set[asyncio.Task] = set()

    async def process(self, f: Filing) -> str:
        if self.store.seen(f.exchange, f.filing_id):
            return "skipped"
        self.store.mark(f.exchange, f.filing_id, f.key, "seen")
        try:
            state = await self._process(f)
        except Exception:
            log.exception("pipeline error for %s %s", f.exchange, f.symbol)
            state = "error"
        self.store.mark(f.exchange, f.filing_id, f.key, state)
        log.info("%s %s %s", f.exchange, f.symbol or f.key, state)
        return state

    def _filtered_out(self, f: Filing) -> bool:
        flt = self.s.filter
        if not flt.enabled:
            return False
        if f.symbol in flt.watchlist or (f.isin and f.isin in flt.watchlist):
            return False
        scrip = (self.store.scrip_by_isin(f.isin) if f.isin else None) or \
                (self.store.scrip_by_code(f.scrip_code) if f.scrip_code else None)
        mcap = scrip.get("mcap_cr") if scrip else None
        if mcap is None:
            return not flt.unknown_mcap_pass
        return float(mcap) < flt.min_mcap_cr

    async def _process(self, f: Filing) -> str:
        if self._filtered_out(f):
            return "skipped"
        async with self._locks.setdefault(f.key, asyncio.Lock()):
            if self.store.alert_today_other_exchange(f.key, f.exchange, self.now().date()):
                return "skipped"
            info = await self._fetch_pdf(f)
            if info is None:
                return "error"
            try:
                return await self._analyse_pdf(f, info)
            finally:
                pdftext.close(info)

    async def _fetch_pdf(self, f: Filing):
        """Download + open; retries twice because exchanges sometimes serve a block page briefly."""
        for attempt, wait in enumerate(self.pdf_retry_waits):
            await asyncio.sleep(wait)
            try:
                async with self._dl:
                    data = await self.http.get_bytes(f.pdf_url, max_bytes=MAX_PDF, timeout=30,
                                                     headers={"Referer": _REFERER.get(f.exchange, "")})
                return pdftext.load_pdf(data)
            except TooLarge as e:
                log.warning("pdf too large %s: %s", f.pdf_url, e)
                return None
            except (HttpError, ValueError) as e:
                if isinstance(e, HttpError) and e.status == 404:
                    break
                log.warning("pdf attempt %d failed %s: %s", attempt + 1, f.pdf_url, e)
        return None

    async def _analyse_pdf(self, f: Filing, info) -> str:
        picked = pdftext.pick_page(pdftext.find_result_pages(info))
        ex, conf = None, None
        if picked:
            ex = extract_rows(info.pages[picked[0]])
            if ex and validate(ex).ok:
                conf = Confidence.VERIFIED
        if conf is None:
            looks_like_result = picked or _RESULT_WORDS.search(f.subject) or \
                any(_RESULT_WORDS.search(t) for t in info.pages[:3])
            first = range(min(8, len(info.pages)))
            pages = ([picked[0]] if picked else pdftext.vision_candidates(info)
                     or [i for i in first if detect_template(info.pages[i])][:2]
                     # scanned table with a little stray OCR text: least-text pages
                     or sorted(first, key=lambda i: len(info.pages[i].strip()))[:2])
            if not pages or not looks_like_result:
                return "no_table"
            ex = None
            if self.router:
                jpegs = [pdftext.render_jpeg(info, i) for i in pages]
                vx = await llm_extract_vision(self.router, jpegs, self.extract_timeout)
                if vx is NOT_FOUND:
                    return "no_table"
                v = validate(vx) if vx else None
                log.info("vision %s: %s", f.symbol or f.key,
                         "no answer" if vx is None else ("ok" if v.ok else f"rejected {v.failed}"))
                if v and v.ok:
                    ex, conf = vx, Confidence.LLM_VERIFIED
            if conf is None:
                conf = Confidence.UNVERIFIED if picked else Confidence.NO_TEXT
                reason = ("Numbers could not be read reliably — open the PDF" if picked
                          else "Scanned PDF — open the PDF")
                period = fallback_period_end(f.disseminated_at.date())
                if self.store.alert_statements(f.key, period):
                    return "skipped"  # already alerted (headline or numbers) for this result
                msg_id = await self.tg.send(format_headline(f, reason, self.now()))
                self.store.save_alert(f.key, period, "headline", f.exchange, f.filing_id, msg_id,
                                      self.now().date())
                self._event(f, None, None, conf)
                return "unverified"
        return await self._alert(f, ex, conf)

    async def _alert(self, f: Filing, ex, conf: Confidence) -> str:
        period = ex.period_end or fallback_period_end(f.disseminated_at.date())
        ex.period_end = period
        existing = self.store.alert_statements(f.key, period)
        existing.discard("headline")  # numbers after an earlier headline-only alert are still sent
        if ex.statement in existing or "consolidated" in existing or \
                (existing and ex.template == Template.BANK):
            return "skipped"
        a = analyse(ex, self.s.scoring or None)
        html = format_flash(f, ex, a, conf, self.now())
        msg_id = await self.tg.send(html)
        self.store.save_alert(f.key, period, ex.statement, f.exchange, f.filing_id, msg_id, self.now().date())
        self._event(f, ex, a, conf)
        if msg_id and self.router:
            t = asyncio.create_task(self._commentary(f, a, html, msg_id))
            self.pending_commentary.add(t)
            t.add_done_callback(self.pending_commentary.discard)
        return "done"

    async def _commentary(self, f: Filing, a, html: str, msg_id: int) -> None:
        try:
            bullets = await llm_commentary(self.router, f.symbol, a, self.commentary_timeout)
            if bullets:
                await self.tg.edit(msg_id, append_ai_view(html, bullets))
        except Exception:
            log.exception("commentary failed for %s", f.symbol)

    def _event(self, f: Filing, ex, a, conf: Confidence) -> None:
        payload = {
            "event_version": 1, "key": f.key, "isin": f.isin, "symbol": f.symbol, "name": f.name,
            "exchange": f.exchange, "filing_id": f.filing_id, "pdf_url": f.pdf_url,
            "disseminated_at": f.disseminated_at.isoformat(), "alerted_at": self.now().isoformat(),
            "confidence": conf.value,
        }
        if ex is not None:
            payload |= {
                "period_end": ex.period_end.isoformat() if ex.period_end else None,
                "fy_label": fy_label(ex.period_end) if ex.period_end else None,
                "statement": ex.statement, "template": ex.template.value, "source": ex.source,
                "rows": {k: [c.value for c in v] for k, v in ex.rows.items()},
            }
        if a is not None:
            payload |= {"score": a.score, "verdict": a.verdict,
                        "metrics": [asdict(m) for m in a.metrics],
                        "flags": [{"positive": fl.positive, "text": fl.text} for fl in a.flags]}
        self.store.add_event(payload)
        if self.s.webhook_url:
            t = asyncio.create_task(self._webhook(payload))
            self.pending_commentary.add(t)
            t.add_done_callback(self.pending_commentary.discard)

    async def _webhook(self, payload: dict) -> None:
        try:
            await self.http.post_json(self.s.webhook_url, payload, timeout=10)
        except Exception as e:
            log.warning("webhook failed: %s", e)
