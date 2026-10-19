import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from resultagent.app import App, poll_interval
from resultagent.config import load_settings
from resultagent.http import HttpError
from resultagent.llm import LlmRouter
from resultagent.models import Cell, Extraction, Filing, Template
from resultagent.pipeline import Pipeline
from resultagent.store import Store
from resultagent.telegram import Telegram
from resultagent.xbrl import XbrlFiling, _quarter_before, parse

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 17, 20, 40, tzinfo=IST)
Q2 = date(2026, 9, 30)

XML = """<xbrli:xbrl>
<xbrli:context id="OneD"><xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="FourD"><xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
<in-capmkt:ISIN contextRef="OneD">INE040A01034</in-capmkt:ISIN>
<in-capmkt:InterestEarned contextRef="OneD" unitRef="INR">834266000000</in-capmkt:InterestEarned>
<in-capmkt:InterestEarned contextRef="FourD" unitRef="INR">1627893800000</in-capmkt:InterestEarned>
<in-capmkt:InterestExpended contextRef="OneD" unitRef="INR">488074200000</in-capmkt:InterestExpended>
<in-capmkt:ProfitLossForThePeriod contextRef="OneD" unitRef="INR">195099600000</in-capmkt:ProfitLossForThePeriod>
<in-capmkt:BasicEarningsPerShareAfterExtraordinaryItems contextRef="OneD">12.66</in-capmkt:BasicEarningsPerShareAfterExtraordinaryItems>
<in-capmkt:PercentageOfGrossNpa contextRef="OneD">0.0111</in-capmkt:PercentageOfGrossNpa>
<in-capmkt:PercentageOfNpa contextRef="OneD">0</in-capmkt:PercentageOfNpa>
</xbrli:xbrl>"""


def test_parse_bank_xbrl():
    template, v, isin = parse(XML, Q2)
    assert template is Template.BANK and isin == "INE040A01034"
    assert v["interest_earned"] == pytest.approx(83426.6) and v["pat"] == pytest.approx(19509.96)
    assert v["eps"] == 12.66 and v["gnpa_pct"] == pytest.approx(1.11)
    assert "nnpa_pct" not in v  # 0 = not reported


def test_quarter_before():
    assert _quarter_before(Q2, 3) == date(2026, 6, 30)
    assert _quarter_before(Q2, 12) == date(2025, 9, 30)
    assert _quarter_before(date(2026, 3, 31), 3) == date(2025, 12, 31)


class FakeTg:
    def __init__(self, ok=True):
        self.sent, self.edits, self.ok = [], [], ok

    async def send(self, html):
        self.sent.append(html)
        return len(self.sent) if self.ok else None

    async def edit(self, mid, html):  # sent[] keeps each message's final text
        self.edits.append(html)
        self.sent[mid - 1] = html
        return True


def xf(statement="consolidated", seq="x1", symbol="ROSSARI"):
    return XbrlFiling(seq, symbol, "Rossari Biotech", statement, Q2, "https://x/a.xml",
                      "https://x/a.html", NOW.replace(minute=38), False)


class FakeXbrl:
    def __init__(self, template=Template.CORPORATE, isin="INE02A801020", statements=("standalone", "consolidated")):
        self.template, self.isin, self.statements = template, isin, statements

    async def history(self, symbol):
        return [xf(s, f"h{i}") for i, s in enumerate(self.statements)]

    async def extraction(self, x, hist=None):
        rows = {"revenue": [697.56, 697.2, 586.11], "total_expenses": [649.36, 653.17, 538.51],
                "finance_cost": [8.68, 10.98, 6.09], "depreciation": [30.1, 25.62, 18.23],
                "pat": [38.91, 35.09, 36.88], "eps": [7.02, 6.34, 6.66]}
        if self.template == Template.BANK:
            rows = {"interest_earned": [100.0, 95.0, 90.0], "interest_expended": [50.0, 48.0, 46.0],
                    "pat": [20.0, 19.0, 18.0]}
        return Extraction(self.template, x.statement, Q2, {k: [Cell(v) for v in vals] for k, vals in rows.items()},
                          "xbrl", "crore"), self.isin


@pytest.fixture
def settings(make_env):
    return load_settings(Path("config.example.toml"), make_env())


def ann(exchange="NSE", fid="1", isin="INE02A801020", subject="has submitted the Financial Results"):
    return Filing(exchange, fid, isin, "ROSSARI", "Rossari Biotech", "https://x/a.pdf", NOW.replace(minute=11), subject)


def make(settings, tmp_path, xbrl=None, tg=None):
    store = Store(tmp_path / "t.db")
    tg = tg or FakeTg()
    return Pipeline(settings, store, xbrl or FakeXbrl(), None, tg, now=lambda: NOW), store, tg


async def test_ping_then_xbrl_edits_same_message(settings, tmp_path):
    p, store, tg = make(settings, tmp_path)
    assert await p.process(ann()) == "pinged"
    assert "results out" in tg.sent[0]
    assert await p.process_xbrl(xf("consolidated")) == "done"
    assert len(tg.sent) == 1 and "EBITDA" in tg.sent[0] and "exchange XBRL" in tg.sent[0]
    ev = json.loads(store._db.execute("select json from events").fetchone()[0])
    assert ev["source"] == "xbrl" and ev["rows"]["pat"][0] == 38.91 and ev["fy_label"] == "Q2 FY27"


async def test_one_ping_across_exchanges_and_none_for_non_results(settings, tmp_path):
    p, _, tg = make(settings, tmp_path)
    assert await p.process(ann("NSE", "1")) == "pinged"
    assert await p.process(ann("BSE", "b1", isin="INE02A801012", subject="Results")) == "skipped"
    assert await p.process(ann("NSE", "2", isin="INE999X01010", subject="Outcome of Board Meeting - dividend")) == "skipped"
    assert len(tg.sent) == 1


async def test_standalone_skipped_when_company_files_consolidated(settings, tmp_path):
    p, _, tg = make(settings, tmp_path)
    assert await p.process_xbrl(xf("standalone", "s1")) == "skipped"
    assert await p.process_xbrl(xf("consolidated", "c1")) == "done"
    assert len(tg.sent) == 1


async def test_bank_uses_standalone(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, FakeXbrl(Template.BANK, "INE040A01034"))
    assert await p.process_xbrl(xf("consolidated", "c1", "HDFCBANK")) == "skipped"
    assert await p.process_xbrl(xf("standalone", "s1", "HDFCBANK")) == "done"
    assert "NII" in tg.sent[0]


async def test_only_standalone_company(settings, tmp_path):
    p, _, tg = make(settings, tmp_path, FakeXbrl(statements=("standalone",)))
    assert await p.process_xbrl(xf("standalone", "s1")) == "done"


async def test_xbrl_seen_and_duplicate(settings, tmp_path):
    p, _, tg = make(settings, tmp_path)
    assert await p.process_xbrl(xf()) == "done"
    assert await p.process_xbrl(xf()) == "skipped"            # same seq id
    assert await p.process_xbrl(xf(seq="x2")) == "skipped"    # same result re-posted
    assert len(tg.sent) == 1


async def test_xbrl_network_error_is_contained(settings, tmp_path):
    class Broken(FakeXbrl):
        async def history(self, symbol):
            raise HttpError(403, "blocked")
    p, _, tg = make(settings, tmp_path, Broken())
    assert await p.process_xbrl(xf()) == "error" and tg.sent == []


async def test_telegram_down_event_still_written(settings, tmp_path):
    p, store, _ = make(settings, tmp_path, tg=FakeTg(ok=False))
    assert await p.process_xbrl(xf()) == "done"
    assert store._db.execute("select count(*) from events").fetchone()[0] == 1


async def test_filter_min_mcap(settings, tmp_path):
    settings.filter.enabled, settings.filter.min_mcap_cr = True, 200000
    p, store, tg = make(settings, tmp_path)
    store.upsert_scrips([{"code": "543213", "isin": "INE02A801020", "symbol": "ROSSARI",
                          "name": "x", "mcap_cr": 1000, "grp": "B"}])
    assert await p.process(ann()) == "skipped"
    assert await p.process_xbrl(xf()) == "skipped"
    assert tg.sent == []


def test_poll_interval(settings):
    assert poll_interval(NOW.replace(hour=10), settings.poll) == 10
    assert poll_interval(NOW.replace(hour=15), settings.poll) == 5
    assert poll_interval(NOW.replace(hour=2), settings.poll) == 60


async def test_startup_marks_existing_seen(settings, tmp_path):
    class Src:
        async def fetch(self, day):
            return [ann(fid=str(i)) for i in range(3)]
    store = Store(tmp_path / "a.db")
    app = App(settings, store=store, telegram=FakeTg(), sources={"NSE": Src()}, now=lambda: NOW)

    async def no_xbrl(size=50):
        return [xf(seq="old")]
    app.xbrl.fetch = no_xbrl
    await app.mark_existing_seen()
    assert all(store.seen("NSE", str(i)) for i in range(3)) and store.seen("XBRL", "old")
    assert await app.poll_once(Src()) == 0 and await app.poll_xbrl() == 0


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
    cfg.models = cfg.models[:2]
    ok = {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 5}}
    http = PostHttp([HttpError(429, "slow down"), ok])
    r = LlmRouter(http, store, cfg)
    assert await r.complete([], vision=False, purpose="extract", timeout=5) == "{}"
    assert len(http.urls) == 2
    store.add_llm_usage(date.today(), cfg.models[1].name, cfg.models[1].rpd, 0)
    assert await r.complete([], vision=False, purpose="extract", timeout=5) is None


async def test_telegram_retry_after():
    slept = []

    async def sleep(s):
        slept.append(s)
    http = PostHttp([HttpError(429, '{"ok":false,"parameters":{"retry_after":3}}'),
                     {"result": {"message_id": 7}}])
    tg = Telegram(http, "t", "1", sleep=sleep)
    assert await tg.send("hi") == 7
    assert 3 in slept


async def test_old_quarter_filing_is_stale(settings, tmp_path):
    p, _, tg = make(settings, tmp_path)
    old = xf(seq="old1")
    old.period_end = date(2025, 12, 31)
    assert await p.process_xbrl(old) == "stale" and tg.sent == []
