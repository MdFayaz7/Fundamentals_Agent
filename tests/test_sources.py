import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from resultagent.http import Http, HttpError, TooLarge
from resultagent.sources import BseSource, NseSource, ScripMaster, is_result_announcement
from resultagent.store import Store

FIX = Path(__file__).parent / "fixtures" / "api"


def load(name):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get_json(self, url, *, headers=None, timeout=20):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"unrouted {url}")


def test_nse_parses_only_results():
    import asyncio

    rows = load("nse_announcements.json")
    http = FakeHttp({"corporate-announcements": rows, "nseindia.com/": {}})
    filings = asyncio.run(NseSource(http).fetch(date(2026, 10, 17)))
    assert filings
    for f in filings:
        assert f.pdf_url.endswith(".pdf")
        assert f.disseminated_at.tzinfo == ZoneInfo("Asia/Kolkata")
    raw = {r["seq_id"]: r for r in rows}
    assert all(is_result_announcement("NSE", raw[f.filing_id]) for f in filings)
    assert len(filings) < len(rows)
    psb = [f for f in filings if f.symbol == "PSB"]
    assert psb and psb[0].isin == "INE608A01012"
    ros = [f for f in filings if f.symbol == "ROSSARI"]
    assert ros and ros[0].isin == "INE02A801020"
    ids = {f.filing_id for f in filings}
    for r in rows:
        if r["desc"] in ("Press Release", "Change in Director(s)"):
            assert r["seq_id"] not in ids


def _bse(tmp_path, rows_extra=()):
    import asyncio

    store = Store(tmp_path / "t.db")
    asyncio.run(ScripMaster(FakeHttp({"ListofScripData": load("bse_scrips.json")}), store).refresh())
    res = load("bse_results.json")
    res["Table"] = list(res["Table"]) + list(rows_extra)
    return store, FakeHttp({"AnnSubCategoryGetData": res})


def _row(code, newsid="x-1"):
    base = next(r for r in load("bse_results.json")["Table"] if r["SUBCATNAME"] == "Financial Results")
    return {**base, "SCRIP_CD": code, "NEWSID": newsid, "RN": 1}


def test_bse_maps_isin_and_url(tmp_path):
    import asyncio

    store, http = _bse(tmp_path, [_row(532174, "icici-1")])
    filings = asyncio.run(BseSource(http, store).fetch(date(2026, 10, 17)))
    f = next(f for f in filings if f.scrip_code == "532174")
    assert f.isin == "INE090A01021"
    assert f.pdf_url.startswith("https://www.bseindia.com/xml-data/corpfiling/AttachLive/")
    assert f.disseminated_at.tzinfo == ZoneInfo("Asia/Kolkata")


def test_bse_skips_sme(tmp_path):
    import asyncio

    store, http = _bse(tmp_path, [_row(534708, "sme-1")])
    assert not any(
        f.scrip_code == "534708" for f in asyncio.run(BseSource(http, store).fetch(date(2026, 10, 17)))
    )
    http2 = FakeHttp({"AnnSubCategoryGetData": http.routes["AnnSubCategoryGetData"]})
    inc = asyncio.run(BseSource(http2, store, include_sme=True).fetch(date(2026, 10, 17)))
    assert any(f.scrip_code == "534708" for f in inc)


def test_nse_html_response_raises_httperror():
    import asyncio

    class HtmlResp:
        status_code = 200
        text = "<html>Access Denied</html>"
        headers = {}

        def json(self):
            raise ValueError("not json")

    class Sess:
        async def request(self, method, url, **kw):
            return HtmlResp()

    src = NseSource(Http(session=Sess()))
    with pytest.raises(HttpError) as ei:
        asyncio.run(src.fetch(date(2026, 10, 17)))
    assert ei.value.status == 200 and "Access Denied" in ei.value.body_snippet
    assert src._warmed_at is None  # marked for re-warm


def test_transport_error_wrapped():
    import asyncio

    class Sess:
        async def request(self, *a, **kw):
            raise TimeoutError("boom")

    with pytest.raises(HttpError) as ei:
        asyncio.run(Http(session=Sess()).get_json("http://x"))
    assert ei.value.status == 0


def test_get_bytes_too_large():
    import asyncio

    class Resp:
        status_code = 200
        headers = {}

        async def aiter_content(self):
            for _ in range(10):
                yield b"x" * 100

        async def aclose(self):
            pass

    class Sess:
        async def request(self, *a, **kw):
            return Resp()

    http = Http(session=Sess())
    with pytest.raises(TooLarge):
        asyncio.run(http.get_bytes("http://x", max_bytes=250))
    assert len(asyncio.run(http.get_bytes("http://x", max_bytes=5000))) == 1000
