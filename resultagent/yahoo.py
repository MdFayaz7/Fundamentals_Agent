"""Yahoo Finance (free, unofficial) for NSE stocks (SYMBOL.NS): valuation, balance-sheet quality,
ownership, analyst view, last ~6 quarters, and technicals computed from 1 year of daily prices.
Every number is computed here; the AI only interprets it. One session + crumb, cached per symbol."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote as urlquote

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)
_Q2 = "https://query2.finance.yahoo.com"
_MODULES = "financialData,defaultKeyStatistics,summaryDetail,assetProfile,price"
_SERIES = "quarterlyTotalRevenue,quarterlyEBITDA,quarterlyOperatingIncome,quarterlyNetIncome"
_CACHE_SECS = 600


def _raw(d: dict, k: str):
    v = d.get(k)
    return v.get("raw") if isinstance(v, dict) else v


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def _ret(closes: list[float], days: int) -> float | None:
    return (closes[-1] / closes[-1 - days] - 1) * 100 if len(closes) > days and closes[-1 - days] else None


class Yahoo:
    def __init__(self):
        self._s: AsyncSession | None = None
        self._crumb: str | None = None
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def _session(self, reset: bool = False) -> tuple[AsyncSession, str]:
        async with self._lock:
            if reset or self._s is None or not self._crumb:
                if self._s:
                    await self._s.close()
                self._s = AsyncSession(impersonate="chrome")
                await self._s.get("https://fc.yahoo.com", timeout=15)  # sets the consent cookie
                self._crumb = (await self._s.get(_Q2 + "/v1/test/getcrumb", timeout=15)).text.strip()
            return self._s, self._crumb

    async def _get(self, path: str) -> dict | None:
        for attempt in (0, 1):
            s, crumb = await self._session(reset=attempt == 1)
            sep = "&" if "?" in path else "?"
            r = await s.get(f"{_Q2}{path}{sep}crumb={crumb}", timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in (401, 403):
                return None
        return None

    async def close(self) -> None:
        if self._s:
            await self._s.close()

    async def search(self, query: str) -> str | None:
        """Best Yahoo ticker for a company name or symbol. Indian (NSE) listing preferred when one
        exists, e.g. 'Reliance' -> RELIANCE.NS, 'INFY' -> INFY.NS, 'Apple' -> AAPL."""
        d = await self._get(f"/v1/finance/search?q={urlquote(query)}&quotesCount=6&newsCount=0")
        eq = [x for x in (d or {}).get("quotes", []) if x.get("quoteType") == "EQUITY" and x.get("symbol")]
        if not eq:
            return None
        base = eq[0]["symbol"].split(".")[0]
        nse = next((x["symbol"] for x in eq if x["symbol"] == f"{base}.NS"), None)
        return nse or eq[0]["symbol"]

    async def stock(self, ticker: str) -> dict | None:
        """All metrics for one Yahoo ticker (RELIANCE.NS, AAPL...), or None when Yahoo doesn't know it."""
        ticker = ticker.upper()
        hit = self._cache.get(ticker)
        if hit and time.monotonic() - hit[0] < _CACHE_SECS:
            return hit[1]
        symbol = ticker[:-3] if ticker.endswith((".NS", ".BO")) else ticker
        url_t = urlquote(ticker)
        now = int(time.time())
        try:
            qs, ts, ch = await asyncio.gather(
                self._get(f"/v10/finance/quoteSummary/{url_t}?modules={_MODULES}"),
                self._get(f"/ws/fundamentals-timeseries/v1/finance/timeseries/{url_t}?type={_SERIES}"
                          f"&period1={now - 2 * 365 * 86400}&period2={now}"),
                self._get(f"/v8/finance/chart/{url_t}?range=1y&interval=1d"))
        except Exception as e:  # network trouble: answer without Yahoo data
            log.warning("yahoo %s failed: %s", symbol, e)
            return None
        out = {"symbol": symbol, "ticker": ticker}
        try:
            res = qs["quoteSummary"]["result"][0]
        except (TypeError, KeyError, IndexError):
            res = None
        try:
            chart_cur = ch["chart"]["result"][0]["meta"].get("currency")
        except (TypeError, KeyError, IndexError):
            chart_cur = None
        cur = ((res or {}).get("price", {}).get("currency") or chart_cur or "INR").upper()
        # Financial statements can be reported in another currency than the share price (e.g. Infosys
        # trades in INR but reports in USD) -> statement amounts use the reporting currency.
        fin = ((res or {}).get("financialData", {}).get("financialCurrency") or cur).upper()
        div = 1e7 if cur == "INR" else 1e6     # market cap: ₹ crore or millions
        fdiv = 1e7 if fin == "INR" else 1e6    # statement amounts
        out["currency"], out["fin_currency"] = cur, fin
        if res:
            fd, ks, sd = res.get("financialData", {}), res.get("defaultKeyStatistics", {}), res.get("summaryDetail", {})
            ap, pr = res.get("assetProfile", {}), res.get("price", {})
            out.update(
                name=pr.get("longName") or pr.get("shortName"), sector=ap.get("sector"), industry=ap.get("industry"),
                mcap=(_raw(sd, "marketCap") or 0) / div or None, summary=(ap.get("longBusinessSummary") or "")[:300],
                pe=_raw(sd, "trailingPE"), fwd_pe=_raw(ks, "forwardPE") or _raw(sd, "forwardPE"),
                ev_ebitda=_raw(ks, "enterpriseToEbitda") if fin == cur else None,  # mixed currencies -> unreliable
                pb=_raw(ks, "priceToBook") if fin == cur else None,
                de=(_raw(fd, "debtToEquity") / 100) if _raw(fd, "debtToEquity") is not None else None,
                roe=(_raw(fd, "returnOnEquity") or 0) * 100 or None,
                cash=(_raw(fd, "totalCash") or 0) / fdiv or None, debt=(_raw(fd, "totalDebt") or 0) / fdiv or None,
                rev_growth=(_raw(fd, "revenueGrowth") * 100) if _raw(fd, "revenueGrowth") is not None else None,
                profit_margin=(_raw(fd, "profitMargins") * 100) if _raw(fd, "profitMargins") is not None else None,
                promoters=(_raw(ks, "heldPercentInsiders") or 0) * 100 or None,
                institutions=(_raw(ks, "heldPercentInstitutions") or 0) * 100 or None,
                analyst=fd.get("recommendationKey"), analysts=_raw(fd, "numberOfAnalystOpinions"),
                target=_raw(fd, "targetMeanPrice"), dma50=_raw(sd, "fiftyDayAverage"), dma200=_raw(sd, "twoHundredDayAverage"))
        # quarterly trend
        q: dict[str, dict[str, float]] = {}
        for item in ((ts or {}).get("timeseries") or {}).get("result") or []:
            key = (item.get("meta", {}).get("type") or [""])[0]
            for p in item.get(key) or []:
                if p and p.get("reportedValue"):
                    q.setdefault(p["asOfDate"], {})[key.replace("quarterly", "")] = p["reportedValue"]["raw"] / fdiv
        out["quarters"] = [(d, q[d]) for d in sorted(q)][-6:]
        # technicals
        try:
            r = ch["chart"]["result"][0]
            quote = r["indicators"]["quote"][0]
            rows = [(c, v) for c, v in zip(quote["close"], quote["volume"]) if c]
            closes, vols = [c for c, _ in rows], [v or 0 for _, v in rows]
            out.update(price=closes[-1], ret_1d=_ret(closes, 1), ret_1w=_ret(closes, 5), ret_1m=_ret(closes, 21),
                       ret_3m=_ret(closes, 63), ret_1y=_ret(closes, len(closes) - 1), rsi=_rsi(closes),
                       high_52w=max(closes), low_52w=min(closes),
                       vol_x=(vols[-1] / (sum(vols[-21:-1]) / 20)) if len(vols) > 21 and sum(vols[-21:-1]) else None,
                       as_of=datetime.fromtimestamp(r["meta"].get("regularMarketTime", 0), timezone.utc).strftime("%d-%b"))
        except (TypeError, KeyError, IndexError, ZeroDivisionError):
            pass
        if len(out) <= 2 and not out.get("quarters"):
            return None
        self._cache[ticker] = (time.monotonic(), out)
        return out


def _n(v, fmt: str = "{:,.1f}") -> str | None:
    return None if v is None else fmt.format(v)


def _money(s: dict, v: float | None, statement: bool = False) -> str | None:
    if v is None:
        return None
    c = s.get("fin_currency", s.get("currency", "INR")) if statement else s.get("currency", "INR")
    s = {"currency": c}
    if c == "INR":
        return f"₹{v:,.0f} Cr"
    sym = "$" if s.get("currency") == "USD" else s.get("currency") + " "
    return f"{sym}{v / 1000:,.1f}B" if abs(v) >= 1000 else f"{sym}{v:,.0f}M"


def describe(s: dict) -> str:
    """Compact evidence block for the AI (multi-line)."""
    L = []
    mc, inr = s.get("mcap"), s.get("currency", "INR") == "INR"
    if mc is None:
        size = None
    elif inr:
        size = "large" if mc >= 100000 else "mid" if mc >= 30000 else "small" if mc >= 1000 else "micro"
    else:
        size = "mega" if mc >= 200000 else "large" if mc >= 10000 else "mid" if mc >= 2000 else "small"
    head = [f"{s['symbol']}" + (f" ({s.get('name')})" if s.get("name") else ""), s.get("industry") or s.get("sector"),
            f"mcap {_money(s, mc)} ({size} cap)" if mc else None, None if inr else f"listed: {s.get('ticker')} ({s.get('currency')})"]
    L.append(" | ".join(x for x in head if x))
    if s.get("summary"):
        L.append("  Business: " + s["summary"])
    if s.get("price"):
        cs = "₹" if inr else ("$" if s.get("currency") == "USD" else s.get("currency", "") + " ")
        t = [f"price {cs}{s['price']:,.2f} (as of {s.get('as_of')})"]
        t += [f"{k} {s[v]:+.1f}%" for k, v in (("1d", "ret_1d"), ("1w", "ret_1w"), ("1m", "ret_1m"),
                                                ("3m", "ret_3m"), ("1y", "ret_1y")) if s.get(v) is not None]
        for name, k in (("50DMA", "dma50"), ("200DMA", "dma200")):
            if s.get(k):
                t.append(f"{(s['price'] / s[k] - 1) * 100:+.0f}% vs {name}")
        if s.get("rsi") is not None:
            t.append(f"RSI {s['rsi']:.0f}")
        if s.get("vol_x"):
            t.append(f"volume {s['vol_x']:.1f}x 20d avg")
        if s.get("high_52w"):
            t.append(f"{(s['price'] / s['high_52w'] - 1) * 100:+.0f}% from 52w high")
        L.append("  Price: " + " | ".join(t))
    v = [x for x in (_n(s.get("pe"), "P/E {:.1f}"), _n(s.get("fwd_pe"), "fwd P/E {:.1f}"),
                     _n(s.get("ev_ebitda"), "EV/EBITDA {:.1f}"), _n(s.get("pb"), "P/B {:.1f}"),
                     _n(s.get("roe"), "ROE {:.1f}%"), _n(s.get("de"), "D/E {:.2f}"),
                     _n(s.get("rev_growth"), "revenue growth (latest qtr YoY) {:+.0f}%"),
                     _n(s.get("profit_margin"), "net margin {:.1f}%"),
                     f"debt {_money(s, s['debt'], True)}" if s.get("debt") else None,
                     f"cash {_money(s, s['cash'], True)}" if s.get("cash") else None) if x]
    if v:
        L.append("  Valuation/quality: " + " | ".join(v))
    o = [x for x in (_n(s.get("promoters"), "promoters/insiders {:.0f}%"), _n(s.get("institutions"), "institutions {:.0f}%")) if x]
    if s.get("analyst") and s.get("analysts"):
        tgt = f", target {s['target']:,.0f} ({(s['target'] / s['price'] - 1) * 100:+.0f}%)" if s.get("target") and s.get("price") else ""
        o.append(f"analysts: {s['analyst'].replace('_', ' ')} ({s['analysts']}){tgt}")
    if o:
        L.append("  Ownership/street: " + " | ".join(o))
    qs = s.get("quarters") or []
    if qs:
        def row(label, key):
            vals = [d.get(key) for _, d in qs]
            return f"{label} " + " → ".join("n/a" if x is None else f"{x:,.0f}" for x in vals) if any(vals) else None
        margins = [(d.get("OperatingIncome") / d["TotalRevenue"] * 100) if d.get("OperatingIncome") is not None
                   and d.get("TotalRevenue") else None for _, d in qs]
        rows = [row("revenue", "TotalRevenue"), row("EBITDA", "EBITDA"), row("net profit", "NetIncome"),
                "op margin " + " → ".join("n/a" if m is None else f"{m:.0f}%" for m in margins) if any(margins) else None]
        fc = s.get("fin_currency", s.get("currency", "INR"))
        unit = "₹ Cr" if fc == "INR" else f"{fc} M"
        L.append(f"  Last {len(qs)} quarters ({qs[0][0][:7]}…{qs[-1][0][:7]}, {unit}): " + " | ".join(r for r in rows if r))
    sig = signals(s)
    if sig:
        L.append("  Signals: " + "; ".join(sig))
    return "\n".join(L)


def signals(s: dict) -> list[str]:
    """Rule-based trader signals computed from the numbers (no AI)."""
    out = []
    rsi, px = s.get("rsi"), s.get("price")
    if rsi is not None:
        out.append("overbought (RSI>70)" if rsi > 70 else "oversold (RSI<30)" if rsi < 30 else "")
    if px and s.get("dma200"):
        out.append("uptrend (above 200DMA)" if px > s["dma200"] else "downtrend (below 200DMA)")
        if px > 1.5 * s["dma200"]:
            out.append("extended: >50% above 200DMA")
    if s.get("vol_x") and s["vol_x"] >= 2:
        out.append(f"strong volume confirmation ({s['vol_x']:.1f}x)")
    revs = [d.get("TotalRevenue") for _, d in (s.get("quarters") or [])][-5:]
    revs = [r for r in revs if r]
    if len(revs) >= 4:
        ups = sum(b > a for a, b in zip(revs, revs[1:]))
        out.append(f"revenue rose in {ups} of last {len(revs) - 1} quarters")
    if s.get("de") is not None and s["de"] > 1.5 and "Bank" not in (s.get("industry") or ""):
        out.append(f"high leverage (D/E {s['de']:.1f})")
    if s.get("pe") and s.get("fwd_pe") and s["fwd_pe"] < 0.7 * s["pe"]:
        out.append("earnings expected to grow strongly (fwd P/E well below trailing)")
    return [x for x in out if x]
