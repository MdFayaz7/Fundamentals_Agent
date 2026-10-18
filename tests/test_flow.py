import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from resultagent.app import App, poll_interval
from resultagent.config import load_settings
from resultagent.http import HttpError
from resultagent.llm import LlmRouter
from resultagent.models import Filing
from resultagent.pipeline import Pipeline
from resultagent.store import Store
from resultagent.telegram import Telegram

IST = ZoneInfo("Asia/Kolkata")
PDF = Path("tests/fixtures/pdf")
NOW = datetime(2026, 10, 17, 20, 40, tzinfo=IST)
HDFC_TRUTH = {"found": True, "template": "bank", "statement": "standalone", "unit": "crore",
              "period_end": "2026-09-30", "rows": {
                  "interest_earned": [83426.60, 79362.78, 76690.70], "other_income": [13831.02, 12821.60, 14350.02],
                  "total_income": [97257.62, 92184.38, 91040.72], "interest_expended": [48807.42, 45828.83, 45139.20],
                  "opex": [18966.78, 18187.49, 17977.92], "ppop": [29483.42, 28168.06, 27923.60],
                  "provisions": [3573.62, 3059.76, 3500.53], "pbt": [25909.80, 25108.30, 24423.07],
                  "tax": [6399.84, 6048.58, 5781.79], "pat": [19509.96, 19059.72, 18641.28]}}


class FakeHttp:
    def __init__(self, pdf: bytes | Exception):
        self.pdf, self.calls = pdf, 0

    async def get_bytes(self, url, **kw):
        self.calls += 1
        if isinstance(self.pdf, Exception):
            raise self.pdf
        return self.pdf


class FakeTg:
    def __init__(self, ok=True):
        self.sent, self.edits, self.ok = [], [], ok

    async def send(self, html):
        self.sent.append(html)
        return len(self.sent) if self.ok else None

    async def edit(self, mid, html):
        self.edits.append(html)
        return True


class FakeRouter:
    def __init__(self, reply=None, delay=0.0):
        self.reply, self.delay = reply, delay

    async def complete(self, messages, *, vision, purpose, timeout):
        await asyncio.sleep(self.delay)
        if purpose == "commentary":
            return json.dumps({"bullets": ["Profit up steadily"]})
        return json.dumps(self.reply) if self.reply else None


@pytest.fixture
def settings(make_env):
    return load_settings(Path("config.example.toml"), make_env())


def filing(exchange="NSE", fid="1", isin="INE040A01034", subject="Financial Results"):
    return Filing(exchange, fid, isin, "HDFCBANK", "HDFC Bank Ltd", "https://x/a.pdf",
                  NOW.replace(minute=39), subject)


def make(settings, tmp_path, pdf, router=None, tg=None):
    store = Store(tmp_path / "t.db")
    tg = tg or FakeTg()
    return Pipeline(settings, store, FakeHttp(pdf), router, tg, now=lambda: NOW), store, tg


async def test_rules_verified_flash(settings, tmp_path):
    p, store, tg = make(settings, tmp_path, (PDF / "rossari_page.pdf").read_bytes())
    assert await p.process(filing(isin="INE02A801020")) == "done"
    assert "EBITDA" in tg.sent[0]
    ev = json.loads(store._db.execute("select json from events").fetchone()[0])
    assert ev["confidence"] == "VERIFIED" and ev["source"] == "rules"


async def test_text_typo_goes_vision(settings, tmp_path):
    p, store, tg = make(settings, tmp_path, (PDF / "hdfc_page.pdf").read_bytes(), FakeRouter(HDFC_TRUTH))
    assert await p.process(filing()) == "done"
    await asyncio.gather(*p.pending_commentary)
    ev = json.loads(store._db.execute("select json from events").fetchone()[0])
    assert ev["confidence"] == "LLM_VERIFIED" and ev["rows"]["pat"][0] == 19509.96
    assert tg.edits and "Profit up steadily" in tg.edits[0]


async def test_no_llm_headline(settings, tmp_path):
    p, store, tg = make(settings, tmp_path, (PDF / "hdfc_page.pdf").read_bytes(), None)
    assert await p.process(filing()) == "unverified"
    assert "results filed" in tg.sent[0]


async def test_llm_timeout_does_not_block(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, (PDF / "hdfc_page.pdf").read_bytes(), FakeRouter(HDFC_TRUTH, delay=5))
    p.extract_timeout = 0.1

    class SlowRouter(FakeRouter):  # router honours its timeout like the real one
        async def complete(self, messages, *, vision, purpose, timeout):
            try:
                return await asyncio.wait_for(super().complete(messages, vision=vision, purpose=purpose,
                                                               timeout=timeout), timeout)
            except asyncio.TimeoutError:
                return None
    p.router = SlowRouter(HDFC_TRUTH, delay=5)
    assert await asyncio.wait_for(p.process(filing()), 2) == "unverified"


async def test_seen_and_cross_exchange_dedupe(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, (PDF / "rossari_page.pdf").read_bytes())
    assert await p.process(filing(isin="X1")) == "done"
    assert await p.process(filing(isin="X1")) == "skipped"
    calls = p.http.calls
    assert await p.process(filing("BSE", "b9", isin="X1")) == "skipped"
    assert p.http.calls == calls and len(tg.sent) == 1


async def test_broken_pdf_and_too_big(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, b"xx")
    p.pdf_retry_waits = (0, 0, 0)
    assert await p.process(filing()) == "error"
    p.http.pdf = HttpError(404, "nf")
    assert await p.process(filing(fid="2")) == "error"
    assert tg.sent == []


async def test_non_result_scan_spends_no_llm(settings, tmp_path):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("L", (600, 800), 255).save(buf, "PDF")
    router = FakeRouter(HDFC_TRUTH)
    p, _, tg = make(settings, tmp_path, buf.getvalue(), router)
    assert await p.process(filing(subject="Outcome of Board Meeting - dividend")) == "no_table"
    assert tg.sent == []


async def test_telegram_down_event_still_written(settings, tmp_path):
    p, store, _ = make(settings, tmp_path, (PDF / "rossari_page.pdf").read_bytes(), tg=FakeTg(ok=False))
    assert await p.process(filing(isin="X2")) == "done"
    assert store._db.execute("select count(*) from events").fetchone()[0] == 1


async def test_filter_min_mcap(settings, tmp_path):
    settings.filter.enabled, settings.filter.min_mcap_cr = True, 200000
    p, store, _ = make(settings, tmp_path, b"")
    store.upsert_scrips([{"code": "500180", "isin": "INE040A01034", "symbol": "HDFCBANK",
                          "name": "x", "mcap_cr": 1000, "grp": "A"}])
    assert await p.process(filing()) == "skipped"


def test_poll_interval(settings):
    assert poll_interval(NOW.replace(hour=10), settings.poll) == 10
    assert poll_interval(NOW.replace(hour=2), settings.poll) == 60


async def test_startup_marks_existing_seen(settings, tmp_path):
    class Src:
        async def fetch(self, day):
            return [filing(fid=str(i)) for i in range(3)]
    store = Store(tmp_path / "a.db")
    app = App(settings, store=store, http=FakeHttp(b""), telegram=FakeTg(), sources={"NSE": Src()},
              now=lambda: NOW)
    await app.mark_existing_seen()
    assert all(store.seen("NSE", str(i)) for i in range(3))
    assert await app.poll_once(Src()) == 0


class PostHttp:
    def __init__(self, replies):
        self.replies, self.urls = list(replies), []

    async def post_json(self, url, payload, **kw):
        self.urls.append(url)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return 200, r, {}


async def test_router_falls_back_and_respects_rpd(settings, tmp_path):
    store = Store(tmp_path / "r.db")
    cfg = settings.llm
    cfg.models = [m for m in cfg.models if m.vision][:2]
    ok = {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 5}}
    http = PostHttp([HttpError(429, "slow down"), ok])
    r = LlmRouter(http, store, cfg)
    assert await r.complete([], vision=True, purpose="extract", timeout=5) == "{}"
    assert cfg.models[1].model in json.dumps(http.urls) or len(http.urls) == 2
    store.add_llm_usage(date.today(), cfg.models[1].name, cfg.models[1].rpd, 0)
    assert await r.complete([], vision=True, purpose="extract", timeout=5) is None  # 1st cooling, 2nd exhausted


async def test_telegram_retry_after():
    slept = []

    async def sleep(s):
        slept.append(s)
    http = PostHttp([HttpError(429, '{"ok":false,"parameters":{"retry_after":3}}'),
                     {"result": {"message_id": 7}}])
    tg = Telegram(http, "t", "1", sleep=sleep)
    assert await tg.send("hi") == 7
    assert 3 in slept


async def test_headline_not_repeated_across_exchanges(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, (PDF / "hdfc_page.pdf").read_bytes(), None)
    assert await p.process(filing("NSE", "n1")) == "unverified"
    assert await p.process(filing("NSE", "n2")) == "skipped"
    assert await p.process(filing("BSE", "b1")) == "skipped"
    assert len(tg.sent) == 1


def test_key_uses_isin_issuer_prefix():
    a = Filing("NSE", "1", "INE528G01019", "YESBANK", "", "", NOW, "")
    b = Filing("BSE", "2", "INE528G01035", "YESBANK", "", "", NOW, "", "532648")
    assert a.key == b.key == "INE528G01"


def test_vision_columns_reordered_by_date_and_strings_parsed():
    from resultagent.llm import _to_extraction
    ex = _to_extraction({"template": "bank", "statement": "standalone", "unit": "crore",
                         "columns": ["2026-09-30", "2025-09-30", "2026-06-30"],
                         "rows": {"pat": ["19,509.96", "18641.28", "(5.0)"], "gnpa_pct": ["1.1%", 1, 1]}})
    assert [c.value for c in ex.rows["pat"]] == [19509.96, -5.0, 18641.28]
    assert ex.period_end == date(2026, 9, 30) and "gnpa_pct" not in ex.rows
