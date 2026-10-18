from __future__ import annotations

import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .http import Http, HttpError
from .models import Filing
from .store import Store

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_NSE_HOME = "https://www.nseindia.com/"
_NSE_API = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&from_date={d}&to_date={d}"
)
_BSE_API = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno={p}&strCat=Result&strPrevDate={d}&strScrip=&strSearch=P&strToDate={d}"
    "&strType=C&subcategory=-1"
)
_BSE_SCRIPS = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
_BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
_NSE_DESCS = {"Outcome of Board Meeting", "Financial Result Updates", "Financial Results"}
_SME_GROUPS = {"M", "MT", "MS"}
_WARM_SECS = 600
_BSE_MAX_PAGES = 5


def is_result_announcement(exchange: str, row: dict) -> bool:
    """BSE: sub-category is Financial Results. NSE: description is one of the
    result-bearing categories; text is not required (real result rows often say
    only "Outcome of Board Meeting held on ..."); the pipeline gates on PDF keywords."""
    if exchange == "BSE":
        return row.get("SUBCATNAME") == "Financial Results"
    return row.get("desc") in _NSE_DESCS


class NseSource:
    def __init__(self, http: Http):
        self._http = http
        self._warmed_at: float | None = None

    async def _warm(self) -> None:
        try:
            await self._http.get_json(_NSE_HOME, headers={"Referer": _NSE_HOME})
        except Exception:
            pass  # homepage is HTML; only the cookies matter
        self._warmed_at = time.monotonic()

    async def fetch(self, day: date) -> list[Filing]:
        if self._warmed_at is None or time.monotonic() - self._warmed_at > _WARM_SECS:
            await self._warm()
        d = day.strftime("%d-%m-%Y")
        try:
            rows = await self._http.get_json(
                _NSE_API.format(d=d), headers={"Referer": _NSE_HOME}
            )
            if not isinstance(rows, list):
                raise HttpError(200, str(rows)[:200])
        except HttpError as exc:
            if exc.status in (0, 200, 401, 403):
                self._warmed_at = None  # re-warm next time
            raise
        out: list[Filing] = []
        for r in rows:
            try:
                if not isinstance(r, dict) or not is_result_announcement("NSE", r):
                    continue
                pdf = r.get("attchmntFile") or ""
                if not pdf.lower().endswith(".pdf"):
                    continue
                out.append(
                    Filing(
                        exchange="NSE",
                        filing_id=str(r["seq_id"]),
                        isin=r.get("sm_isin"),
                        symbol=r.get("symbol") or "",
                        name=r.get("sm_name") or "",
                        pdf_url=pdf,
                        disseminated_at=datetime.strptime(
                            r["exchdisstime"], "%d-%b-%Y %H:%M:%S"
                        ).replace(tzinfo=IST),
                        subject=r.get("attchmntText") or "",
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("skipping bad NSE row %r: %r", r.get("seq_id") if isinstance(r, dict) else r, exc)
        return out


class BseSource:
    def __init__(self, http: Http, store: Store, include_sme: bool = False):
        self._http = http
        self._store = store
        self._include_sme = include_sme

    async def fetch(self, day: date) -> list[Filing]:
        d = day.strftime("%Y%m%d")
        headers = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
        out: list[Filing] = []
        ids: set[str] = set()
        seen_rows = 0
        for page in range(1, _BSE_MAX_PAGES + 1):
            data = await self._http.get_json(_BSE_API.format(p=page, d=d), headers=headers)
            if not isinstance(data, dict) or not isinstance(data.get("Table") or [], list):
                raise HttpError(200, str(data)[:200])
            table = data.get("Table") or []
            if not table:
                break
            try:
                total = int(((data.get("Table1") or [{}])[0]).get("ROWCNT") or 0)
                seen_rows = max(seen_rows, max(int(r.get("RN") or 0) for r in table))
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                raise HttpError(200, f"bad paging fields: {exc!r}") from exc
            for r in table:
                try:
                    f = self._to_filing(r)
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    log.warning("skipping bad BSE row %r: %r", r.get("NEWSID") if isinstance(r, dict) else r, exc)
                    continue
                if f and f.filing_id not in ids:
                    ids.add(f.filing_id)
                    out.append(f)
            if seen_rows >= total:
                break
        return out

    def _to_filing(self, r: dict) -> Filing | None:
        if not is_result_announcement("BSE", r) or not r.get("ATTACHMENTNAME"):
            return None
        code = str(r["SCRIP_CD"])
        scrip = self._store.scrip_by_code(code)
        if scrip and scrip.get("grp") in _SME_GROUPS and not self._include_sme:
            return None
        return Filing(
            exchange="BSE",
            filing_id=r["NEWSID"],
            isin=scrip["isin"] if scrip else None,
            symbol=scrip["symbol"] if scrip else "",
            name=r.get("SLONGNAME") or "",
            pdf_url=_BSE_PDF + r["ATTACHMENTNAME"],
            disseminated_at=datetime.fromisoformat(r["DissemDT"]).replace(tzinfo=IST),
            subject=r.get("NEWSSUB") or "",
            scrip_code=code,
        )


class ScripMaster:
    def __init__(self, http: Http, store: Store):
        self._http = http
        self._store = store

    async def refresh(self) -> int:
        data = await self._http.get_json(
            _BSE_SCRIPS,
            headers={"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"},
            timeout=60,
        )
        rows = []
        for r in data:
            try:
                mcap = float(r.get("Mktcap")) if r.get("Mktcap") not in (None, "") else None
            except ValueError:
                mcap = None
            rows.append(
                {
                    "code": str(r["SCRIP_CD"]),
                    "isin": r.get("ISIN_NUMBER"),
                    "symbol": r.get("scrip_id"),
                    "name": r.get("Scrip_Name"),
                    "mcap_cr": mcap,
                    "grp": r.get("GROUP"),
                }
            )
        self._store.upsert_scrips(rows)
        return len(rows)
