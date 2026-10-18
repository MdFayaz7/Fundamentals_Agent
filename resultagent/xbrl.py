"""NSE integrated-filing XBRL: the feed of new results and an exact parser for each file.

Each XBRL holds only the current quarter (+ year-to-date); previous-quarter and year-ago numbers
come from the company's earlier XBRL files (fetched once, cached in SQLite).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .http import Http, HttpError
from .models import Cell, Extraction, Template
from .store import Store

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_FEED = ("https://www.nseindia.com/api/integrated-filing-results?index=equities"
         "&type=Integrated%20Filing-%20Financials&size={size}")
_HISTORY = _FEED.replace("&size={size}", "&symbol={symbol}")
_REF = {"Referer": "https://www.nseindia.com/companies-listing/corporate-integrated-filing"}

# our row key -> XBRL element(s), first present wins. Amounts are absolute rupees.
_COMMON = {
    "other_income": ["OtherIncome"], "total_income": ["Income"], "tax": ["TaxExpense"],
}
_MAP = {
    Template.CORPORATE: {
        **_COMMON,
        "revenue": ["RevenueFromOperations"], "total_expenses": ["Expenses"],
        "finance_cost": ["FinanceCosts"], "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
        "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
        "exceptional": ["ExceptionalItemsBeforeTax"], "pbt": ["ProfitBeforeTax"],
        "associates": ["ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],
        "pat": ["ProfitLossForPeriod"], "pat_owners": ["ProfitOrLossAttributableToOwnersOfParent"],
        "eps": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                "BasicEarningsLossPerShareFromContinuingOperations"],
    },
    Template.BANK: {
        **_COMMON,
        "interest_earned": ["InterestEarned"], "interest_expended": ["InterestExpended"],
        "opex": ["OperatingExpenses"], "ppop": ["OperatingProfitBeforeProvisionAndContingencies"],
        "provisions": ["ProvisionsOtherThanTaxAndContingencies"], "exceptional": ["ExceptionalItems"],
        "pbt": ["ProfitLossFromOrdinaryActivitiesBeforeTax"], "pat": ["ProfitLossForThePeriod"],
        "eps": ["BasicEarningsPerShareAfterExtraordinaryItems"],
        "gnpa_pct": ["PercentageOfGrossNpa"], "nnpa_pct": ["PercentageOfNpa"],
    },
}
_MAP[Template.LENDER] = {**_MAP[Template.CORPORATE], "interest_income": ["InterestIncome"],
                         "impairment": ["ImpairmentOnFinancialInstruments"]}
_UNSCALED = {"eps"}
_PCT = {"gnpa_pct", "nnpa_pct"}


@dataclass(slots=True)
class XbrlFiling:
    seq_id: str
    symbol: str
    name: str
    statement: str          # standalone | consolidated
    period_end: date
    xbrl_url: str
    page_url: str           # human-readable iXBRL page
    filed_at: datetime
    revised: bool


def _date(s: str | None, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(s, fmt) if s else None
    except ValueError:
        return None


def to_filing(r: dict) -> XbrlFiling | None:
    qe = _date(r.get("qe_Date"), "%d-%b-%Y")
    filed = _date(r.get("broadcast_Date") or r.get("creation_Date"), "%d-%b-%Y %H:%M:%S")
    if not (qe and filed and r.get("xbrl") and r.get("symbol")) or not r["xbrl"].endswith(".xml"):
        return None
    return XbrlFiling(str(r.get("seq_Id")), r["symbol"], r.get("cmName") or r["symbol"],
                      "consolidated" if (r.get("consolidated") or "").lower().startswith("cons") else "standalone",
                      qe.date(), r["xbrl"], r.get("ixbrl") or r["xbrl"], filed.replace(tzinfo=IST),
                      (r.get("type_Sub") or "").lower().startswith("rev"))


def _quarter_context(xml: str, period_end: date) -> str:
    """Id of the plain (no dimension) ~3-month context ending at period_end. Usually 'OneD'."""
    for cid, body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', xml, re.S):
        if "xbrldi:" in body:
            continue
        s = re.search(r"<xbrli:startDate>([\d-]+)<", body)
        e = re.search(r"<xbrli:endDate>([\d-]+)<", body)
        if s and e and date.fromisoformat(e.group(1)) == period_end and \
                80 <= (period_end - date.fromisoformat(s.group(1))).days <= 100:
            return cid
    return "OneD"


def parse(xml: str, period_end: date) -> tuple[Template, dict[str, float], str | None]:
    """(template, {row_key: value in ₹ crore / EPS in ₹ / NPA in %}, ISIN) for the quarter."""
    m = re.search(r"<in-capmkt:ISIN [^>]*>\s*([A-Z0-9]{12})\s*<", xml)
    isin = m.group(1) if m else None
    ctx = _quarter_context(xml, period_end)
    facts: dict[str, float] = {}
    for name, val in re.findall(rf'<in-capmkt:(\w+) contextRef="{re.escape(ctx)}"[^>]*>([^<]+)<', xml):
        try:
            facts.setdefault(name, float(val))
        except ValueError:
            pass
    if "InterestEarned" in facts and "InterestExpended" in facts:
        template = Template.BANK
    elif "ImpairmentOnFinancialInstruments" in facts and "InterestIncome" in facts:
        template = Template.LENDER
    else:
        template = Template.CORPORATE
    out = {}
    for key, names in _MAP[template].items():
        v = next((facts[n] for n in names if n in facts), None)
        if v is None:
            continue
        if key in _PCT:
            if v == 0:  # banks file NPA ratios only in standalone; 0 means "not reported"
                continue
            v = v * 100 if v < 1 else v
        elif key not in _UNSCALED:
            v = v / 1e7
        out[key] = v
    return template, out, isin


def _quarter_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    first_next = date(y + (m + 1) // 12, (m + 1) % 12 + 1, 1)
    return first_next - timedelta(days=1)


class XbrlSource:
    def __init__(self, http: Http, store: Store):
        self.http, self.store = http, store

    async def fetch(self, size: int = 50) -> list[XbrlFiling]:
        data = await self.http.get_json(_FEED.format(size=size), headers=_REF)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise HttpError(200, str(data)[:200])
        return [f for f in (to_filing(r) for r in rows if isinstance(r, dict)) if f]

    async def history(self, symbol: str) -> list[XbrlFiling]:
        data = await self.http.get_json(_HISTORY.format(symbol=symbol), headers=_REF)
        rows = data.get("data", []) if isinstance(data, dict) else []
        return [f for f in (to_filing(r) for r in rows if isinstance(r, dict)) if f]

    async def _values(self, f: XbrlFiling) -> tuple[Template, dict[str, float], str | None]:
        cached = self.store.xbrl_get(f.symbol, f.statement, f.period_end)
        if cached and not f.revised:
            return Template(cached["template"]), cached["values"], cached.get("isin")
        xml = (await self.http.get_bytes(f.xbrl_url, max_bytes=10 << 20, headers=_REF)).decode("utf-8", "ignore")
        template, values, isin = parse(xml, f.period_end)
        self.store.xbrl_put(f.symbol, f.statement, f.period_end,
                            {"template": template.value, "values": values, "isin": isin})
        return template, values, isin

    async def extraction(self, f: XbrlFiling, hist: list[XbrlFiling] | None = None
                         ) -> tuple[Extraction, str | None]:
        """(Extraction, ISIN). Current quarter from this filing; previous quarter and year-ago
        quarter from the company's earlier XBRL files."""
        template, cur, isin = await self._values(f)
        cols = [cur, {}, {}]
        if hist is None:
            try:
                hist = await self.history(f.symbol)
            except HttpError as e:
                log.warning("history %s failed: %s", f.symbol, e)
                hist = []
        for col, months in ((1, 3), (2, 12)):
            want = _quarter_before(f.period_end, months)
            match = [h for h in hist if h.statement == f.statement and h.period_end == want]
            if match:
                match.sort(key=lambda h: (h.revised, h.filed_at))  # latest revision wins
                try:
                    cols[col] = (await self._values(match[-1]))[1]
                except HttpError as e:
                    log.warning("history xbrl %s %s failed: %s", f.symbol, want, e)
        keys = set().union(*cols)
        rows = {k: [Cell(c.get(k)) for c in cols] for k in keys}
        return Extraction(template=template, statement=f.statement, period_end=f.period_end,
                          rows=rows, source="xbrl", unit="crore"), isin
