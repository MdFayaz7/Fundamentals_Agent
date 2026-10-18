"""Announcement -> instant "results out" ping.  XBRL -> exact report (edits the ping). Never raises."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .analyse import analyse
from .config import Settings
from .fmt import append_ai_view, format_flash, format_instant
from .http import HttpError
from .llm import LlmRouter, llm_commentary
from .models import Confidence, Filing, Template
from .periods import fallback_period_end, fy_label
from .store import Store
from .telegram import Telegram
from .xbrl import XbrlFiling, XbrlSource

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
STALE_DAYS = 120  # results are due within 45 days (60 for Q4)
_RESULT_WORDS = re.compile(r"financial\s+results?|statement\s+of\s+.{0,40}results|un-?audited", re.I)


def isin_key(isin: str | None, fallback: str) -> str:
    # ISIN chars 0-8 = country + issuer + security type (NSE and BSE may differ in the last digits).
    return isin[:9] if isin else fallback


class Pipeline:
    commentary_timeout = 20.0

    def __init__(self, settings: Settings, store: Store, xbrl: XbrlSource, router: LlmRouter | None,
                 telegram: Telegram, now=lambda: datetime.now(IST)):
        self.s, self.store, self.xbrl, self.router, self.tg, self.now = settings, store, xbrl, router, telegram, now
        self._locks: dict[str, asyncio.Lock] = {}
        self.pending: set[asyncio.Task] = set()

    # ---------- filter ----------
    def _filtered_out(self, symbol: str, isin: str | None, scrip_code: str | None = None) -> bool:
        flt = self.s.filter
        if not flt.enabled or symbol in flt.watchlist or (isin and isin in flt.watchlist):
            return False
        scrip = (self.store.scrip_by_isin(isin) if isin else None) or \
                (self.store.scrip_by_code(scrip_code) if scrip_code else None)
        mcap = scrip.get("mcap_cr") if scrip else None
        if mcap is None:
            return not flt.unknown_mcap_pass
        return float(mcap) < flt.min_mcap_cr

    # ---------- 1. announcement: instant ping ----------
    async def process(self, f: Filing) -> str:
        if self.store.seen(f.exchange, f.filing_id):
            return "skipped"
        self.store.mark(f.exchange, f.filing_id, f.key, "seen")
        state = "skipped"
        try:
            is_result = f.exchange == "BSE" or bool(_RESULT_WORDS.search(f.subject))
            period = fallback_period_end(f.disseminated_at.date())
            if is_result and not self._filtered_out(f.symbol, f.isin, f.scrip_code):
                async with self._locks.setdefault(f.key, asyncio.Lock()):
                    if not self.store.alert_statements(f.key, period):
                        mid = await self.tg.send(format_instant(f, self.now()))
                        self.store.save_alert(f.key, period, "ping", f.exchange, f.filing_id, mid,
                                              self.now().date())
                        state = "pinged"
        except Exception:
            log.exception("ping failed for %s", f.symbol)
            state = "error"
        self.store.mark(f.exchange, f.filing_id, f.key, state)
        log.info("%s %s %s %.0fs after filing", f.exchange, f.symbol or f.key, state,
                 (self.now() - f.disseminated_at).total_seconds())
        return state

    # ---------- 2. XBRL: exact report ----------
    async def process_xbrl(self, x: XbrlFiling) -> str:
        if self.store.seen("XBRL", x.seq_id):
            return "skipped"
        self.store.mark("XBRL", x.seq_id, x.symbol, "seen")
        try:
            state = await self._report(x)
        except HttpError as e:
            log.warning("xbrl %s failed: %s", x.symbol, e)
            state = "error"
        except Exception:
            log.exception("xbrl %s failed", x.symbol)
            state = "error"
        self.store.mark("XBRL", x.seq_id, x.symbol, state)
        log.info("XBRL %s %s %s %.0fs after filing", x.symbol, x.statement, state,
                 (self.now() - x.filed_at).total_seconds())
        return state

    async def _report(self, x: XbrlFiling) -> str:
        if (x.filed_at.date() - x.period_end).days > STALE_DAYS:
            return "stale"  # late filing of an old quarter, not news
        hist = await self.xbrl.history(x.symbol)
        ex, isin = await self.xbrl.extraction(x, hist)
        key = isin_key(isin, f"NSE:{x.symbol}")
        if self._filtered_out(x.symbol, isin):
            return "skipped"
        # One report per result: banks -> standalone, others -> consolidated when they file one.
        files_consolidated = any(h.statement == "consolidated" for h in hist)
        preferred = "standalone" if ex.template == Template.BANK or not files_consolidated else "consolidated"
        if x.statement != preferred:
            return "skipped"
        async with self._locks.setdefault(key, asyncio.Lock()):
            if x.statement in self.store.alert_statements(key, x.period_end) and not x.revised:
                return "skipped"
            f = Filing("NSE", x.seq_id, isin, x.symbol, x.name, x.page_url, x.filed_at, "XBRL", None)
            a = analyse(ex, self.s.scoring or None)
            html = format_flash(f, ex, a, Confidence.VERIFIED, self.now())
            mid = self.store.alert_message_id(key, x.period_end)
            if not (mid and await self.tg.edit(mid, html)):
                mid = await self.tg.send(html)
            self.store.save_alert(key, x.period_end, x.statement, "XBRL", x.seq_id, mid, self.now().date())
            self._event(f, key, ex, a)
            if mid and self.router:
                self._spawn(self._commentary(x.symbol, a, html, mid))
        return "done"

    # ---------- helpers ----------
    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    async def _commentary(self, symbol: str, a, html: str, mid: int) -> None:
        try:
            bullets = await llm_commentary(self.router, symbol, a, self.commentary_timeout)
            if bullets:
                await self.tg.edit(mid, append_ai_view(html, bullets))
        except Exception:
            log.exception("commentary failed for %s", symbol)

    def _event(self, f: Filing, key: str, ex, a) -> None:
        payload = {
            "event_version": 2, "key": key, "isin": f.isin, "symbol": f.symbol, "name": f.name,
            "source": "xbrl", "confidence": Confidence.VERIFIED.value, "filing_id": f.filing_id,
            "filed_at": f.disseminated_at.isoformat(), "alerted_at": self.now().isoformat(),
            "period_end": ex.period_end.isoformat(), "fy_label": fy_label(ex.period_end),
            "statement": ex.statement, "template": ex.template.value,
            "rows": {k: [c.value for c in v] for k, v in ex.rows.items()},
            "score": a.score, "verdict": a.verdict, "metrics": [asdict(m) for m in a.metrics],
            "flags": [{"positive": fl.positive, "text": fl.text} for fl in a.flags],
        }
        self.store.add_event(payload)
        if self.s.webhook_url:
            self._spawn(self._webhook(payload))

    async def _webhook(self, payload: dict) -> None:
        try:
            await self.xbrl.http.post_json(self.s.webhook_url, payload, timeout=10)
        except Exception as e:
            log.warning("webhook failed: %s", e)
