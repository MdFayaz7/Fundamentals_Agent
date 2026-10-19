"""Live market + valuation data from BSE's public quote APIs (price, P/E, P/B, ROE, margins, 52-week
range, market cap). Symbol -> BSE scrip code via BSE's scrip master (refreshed daily, kept in SQLite)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from .http import Http, HttpError
from .store import Store, now_ist

log = logging.getLogger(__name__)
_API = "https://api.bseindia.com/BseIndiaAPI/api/"
_HDR = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
_MASTER = _API + "ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active"
_CACHE_SECS = 600


def _f(v) -> float | None:
    try:
        return float(str(v).replace(",", "").split()[0])
    except (TypeError, ValueError, IndexError):
        return None


class Market:
    def __init__(self, http: Http, store: Store):
        self.http, self.store = http, store
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def ensure_master(self) -> None:
        """Download BSE's list of ~5,000 equities once a day (symbol, scrip code, name, market cap)."""
        last = self.store.get("scrips_at")
        if last and now_ist().isoformat() < (last[:10] + "T23:59") and self.store.scrip_count():
            return
        async with self._lock:
            rows = await self.http.get_json(_MASTER, headers=_HDR, timeout=40)
            if isinstance(rows, list) and rows:
                self.store.save_scrips([(r.get("SCRIP_CD"), (r.get("scrip_id") or "").upper(),
                                         r.get("Scrip_Name") or "", r.get("ISIN_NUMBER") or "")
                                        for r in rows if r.get("SCRIP_CD")])
                self.store.set("scrips_at", now_ist().isoformat())

    async def snapshot(self, symbol: str) -> dict | None:
        """Key numbers for one stock, or None if unknown. Cached 10 minutes."""
        symbol = symbol.upper()
        hit = self._cache.get(symbol)
        if hit and time.monotonic() - hit[0] < _CACHE_SECS:
            return hit[1]
        try:
            await self.ensure_master()
        except HttpError as e:
            log.warning("scrip master failed: %s", e)
        code = self.store.scrip_code(symbol)
        if not code:
            return None
        urls = [f"ComHeadernew/w?quotetype=EQ&scripcode={code}&seriesid=",
                f"getScripHeaderData/w?Debtflag=&scripcode={code}&seriesid=",
                f"StockTrading/w?flag=&quotetype=EQ&scripcode={code}",
                f"HighLow/w?Type=EQ&flag=C&scripcode={code}"]
        res = await asyncio.gather(*(self.http.get_json(_API + u, headers=_HDR, timeout=15) for u in urls),
                                   return_exceptions=True)
        head, quote, trade, hl = (r if isinstance(r, dict) else {} for r in res)
        cur = (quote.get("CurrRate") or {})
        snap = {
            "symbol": symbol, "name": (quote.get("Cmpname") or {}).get("FullN") or symbol,
            "sector": head.get("Sector") or head.get("Industry"), "industry": head.get("ISubGroup") or head.get("Industry"),
            "price": _f(cur.get("LTP")), "day_change_pct": _f(cur.get("PcChg")),
            "pe": _f(head.get("PE")), "pb": _f(head.get("PB")), "roe": _f(head.get("ROE")),
            "eps_ttm": _f(head.get("EPS")), "opm": _f(head.get("OPM")), "npm": _f(head.get("NPM")),
            "mcap_cr": _f(trade.get("MktCapFull")),
            "high_52w": _f(hl.get("Fifty2WkHigh_adj")), "low_52w": _f(hl.get("Fifty2WkLow_adj")),
        }
        if snap["price"] is None and snap["pe"] is None:
            return None
        self._cache[symbol] = (time.monotonic(), snap)
        return snap

    async def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        out = await asyncio.gather(*(self.snapshot(s) for s in symbols), return_exceptions=True)
        return {s: o for s, o in zip(symbols, out) if isinstance(o, dict)}


def describe(s: dict) -> str:
    """One compact line for the AI."""
    parts = [f"{s['symbol']} ({s['name']}, {s.get('industry') or s.get('sector') or 'n/a'})"]
    if s.get("price") is not None:
        p = f"price ₹{s['price']:,.2f}"
        if s.get("day_change_pct") is not None:
            p += f" ({s['day_change_pct']:+.2f}% today)"
        parts.append(p)
    if s.get("mcap_cr"):
        m = s["mcap_cr"]  # approx. SEBI buckets: large > ₹1 lakh Cr, mid ₹30k–1 lakh Cr, small below
        size = "large cap" if m >= 100000 else "mid cap" if m >= 30000 else "small cap" if m >= 1000 else "micro cap"
        parts.append(f"mcap ₹{m:,.0f} Cr ({size})")
    for k, label in (("pe", "P/E"), ("pb", "P/B"), ("roe", "ROE %"), ("opm", "OPM %"), ("npm", "NPM %")):
        if s.get(k) is not None:
            parts.append(f"{label} {s[k]:g}")
    hi, lo, px = s.get("high_52w"), s.get("low_52w"), s.get("price")
    if hi and lo and px:
        parts.append(f"52w {lo:g}–{hi:g} ({(px / hi - 1) * 100:+.0f}% from high, {(px / lo - 1) * 100:+.0f}% from low)")
    return " | ".join(parts)
