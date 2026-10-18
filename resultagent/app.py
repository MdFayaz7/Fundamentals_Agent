"""Main loop: two exchange pollers, daily scrip refresh, heartbeat, failure alerts; plus replay."""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .http import Http
from .llm import LlmRouter
from .pipeline import Pipeline
from .xbrl import XbrlSource
from .sources import BseSource, NseSource, ScripMaster
from .store import Store
from .telegram import Telegram

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
FAIL_ALERT_SECS = 600
MAX_BACKOFF = 300
XBRL_POLL_SECS = 30


def poll_interval(now_ist: datetime, cfg) -> float:
    hm = now_ist.strftime("%H:%M")
    if cfg.peak_start <= hm <= cfg.peak_end:
        return cfg.peak_seconds
    return cfg.fast_seconds if cfg.fast_start <= hm <= cfg.fast_end else cfg.slow_seconds


class App:
    def __init__(self, settings: Settings, store: Store | None = None, http: Http | None = None,
                 telegram=None, sources: dict | None = None, now=lambda: datetime.now(IST)):
        self.s, self.now = settings, now
        self.store = store or Store(settings.paths.db)
        self.http = http or Http()
        self.tg = telegram or Telegram(self.http, settings.telegram.bot_token, settings.telegram.chat_id)
        self.router = LlmRouter(self.http, self.store, settings.llm) if settings.llm.models else None
        self.xbrl = XbrlSource(self.http, self.store)
        self.pipeline = Pipeline(settings, self.store, self.xbrl, self.router, self.tg, now)
        self.scrips = ScripMaster(self.http, self.store)
        self.sources = sources or {"NSE": NseSource(self.http),
                                   "BSE": BseSource(self.http, self.store, settings.include_sme)}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _days(self) -> list[date]:
        now = self.now()
        days = [now.date()]
        if now.hour == 0 and now.minute < 15:  # catch late filings from yesterday
            days.append(now.date() - timedelta(days=1))
        return days

    async def mark_existing_seen(self) -> None:
        for name, src in self.sources.items():
            try:
                for f in await src.fetch(self.now().date()):
                    if not self.store.seen(f.exchange, f.filing_id):
                        self.store.mark(f.exchange, f.filing_id, f.key, "seen")
            except Exception as e:
                log.warning("startup fetch %s failed: %s", name, e)
        try:
            for x in await self.xbrl.fetch(100):
                if not self.store.seen("XBRL", x.seq_id):
                    self.store.mark("XBRL", x.seq_id, x.symbol, "seen")
        except Exception as e:
            log.warning("startup fetch XBRL failed: %s", e)

    async def poll_xbrl(self) -> int:
        n = 0
        for x in await self.xbrl.fetch(50):
            if not self.store.seen("XBRL", x.seq_id):
                self._spawn(self.pipeline.process_xbrl(x))
                n += 1
        return n

    async def poll_once(self, src) -> int:
        n = 0
        for day in self._days():
            for f in await src.fetch(day):
                if not self.store.seen(f.exchange, f.filing_id):
                    self._spawn(self.pipeline.process(f))
                    n += 1
        return n

    async def poll_loop(self, name: str) -> None:
        src, failing_since, alerted, fails = self.sources.get(name), None, False, 0
        while True:
            try:
                await (self.poll_xbrl() if name == "XBRL" else self.poll_once(src))
                if alerted:
                    await self.tg.send(f"✅ {name} feed recovered")
                failing_since, alerted, fails = None, False, 0
                base = XBRL_POLL_SECS if name == "XBRL" else poll_interval(self.now(), self.s.poll)
                wait = base * random.uniform(0.8, 1.2)
            except Exception as e:
                fails += 1
                failing_since = failing_since or time.monotonic()
                log.warning("%s poll failed (%d): %s", name, fails, e)
                if not alerted and time.monotonic() - failing_since >= FAIL_ALERT_SECS:
                    await self.tg.send(f"⚠️ {name} feed failing for 10 min: {str(e)[:200]}")
                    alerted = True
                wait = min(MAX_BACKOFF, poll_interval(self.now(), self.s.poll) * 2 ** min(fails, 6))
            await asyncio.sleep(wait)

    async def daily_loop(self) -> None:
        """Scrip master at start and 07:00 IST; heartbeat at 08:30 IST."""
        last_scrips = last_beat = None
        while True:
            now = self.now()
            if last_scrips is None or (now.strftime("%H:%M") >= "07:00" and last_scrips != now.date()):
                try:
                    log.info("scrip master: %d rows", await self.scrips.refresh())
                    last_scrips = now.date()
                except Exception as e:
                    log.warning("scrip refresh failed: %s", e)
            if now.strftime("%H:%M") >= "08:30" and last_beat != now.date():
                st = self.store.stats_for_day(now.date() - timedelta(days=1))
                await self.tg.send(f"✅ Results agent alive · yesterday: {st['alerts']} alerts, "
                                   f"{st['unverified']} unread PDFs, {st['errors']} errors")
                last_beat = now.date()
            await asyncio.sleep(60)

    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        if not self.s.backfill_on_start:
            await self.mark_existing_seen()
        tasks = [asyncio.create_task(c) for c in
                 (self.poll_loop("NSE"), self.poll_loop("BSE"), self.poll_loop("XBRL"), self.daily_loop())]
        log.info("results agent running")
        await stop.wait()
        for t in tasks + list(self._tasks):
            t.cancel()
        await self.http.close()
        self.store.close()


class _PrintTelegram:
    def __init__(self):
        self.n = 0

    async def send(self, html: str) -> int:
        self.n += 1
        print("\n" + html + "\n" + "-" * 60)
        return self.n

    async def edit(self, message_id: int, html: str) -> bool:
        print(f"\n[edit #{message_id}]\n{html}\n" + "-" * 60)
        return True


async def replay(settings: Settings, day: date, send: bool) -> None:
    """Re-process one day's filings with a throwaway DB; prints messages unless --send."""
    tmp = Path(tempfile.mkdtemp()) / "replay.db"
    http = Http()
    store = Store(tmp)
    tg = Telegram(http, settings.telegram.bot_token, settings.telegram.chat_id) if send else _PrintTelegram()
    app = App(settings, store=store, http=http, telegram=tg)
    await app.scrips.refresh()
    rows = []
    for name, src in app.sources.items():
        for f in await src.fetch(day):
            t0 = time.monotonic()
            state = await app.pipeline.process(f)
            rows.append((f.exchange, f.symbol or f.key, state, round(time.monotonic() - t0, 1)))
    for x in await app.xbrl.fetch(300):
        if x.filed_at.date() == day:
            t0 = time.monotonic()
            state = await app.pipeline.process_xbrl(x)
            rows.append(("XBRL", f"{x.symbol} {x.statement[:4]}", state, round(time.monotonic() - t0, 1)))
    await asyncio.gather(*app.pipeline.pending, return_exceptions=True)
    print(f"\n{'exch':4} {'symbol':14} {'state':10} secs")
    for r in rows:
        print(f"{r[0]:4} {r[1][:14]:14} {r[2]:10} {r[3]}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print("totals:", counts)
    await http.close()
    store.close()
