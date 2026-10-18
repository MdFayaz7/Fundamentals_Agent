"""Accuracy check: process one day's filings WITHOUT any LLM (rules + local OCR only), then compare
every number the agent would have sent against the company's own XBRL filed on NSE.

    .venv/bin/python scripts/accuracy_check.py 2026-10-17
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resultagent.app import App  # noqa: E402
from resultagent.config import load_settings  # noqa: E402
from resultagent.http import Http  # noqa: E402
from resultagent.store import Store  # noqa: E402

XBRL_LIST = ("https://www.nseindia.com/api/integrated-filing-results?index=equities"
             "&type=Integrated%20Filing-%20Financials&symbol={sym}")
REF = {"Referer": "https://www.nseindia.com/"}
# our row key -> XBRL element(s) for the quarter (context OneD)
FIELDS = {
    "revenue": ["RevenueFromOperations"],
    "total_income": ["Income"],
    "interest_earned": ["InterestEarned"],
    "pat": ["ProfitLossForPeriod", "ProfitLossForThePeriod"],
}


class Quiet:
    async def send(self, html):
        return 1

    async def edit(self, mid, html):
        return True


def facts(xml: str) -> dict[str, float]:
    out = {}
    for name, val in re.findall(r'<in-capmkt:(\w+) contextRef="OneD"[^>]*>([^<]+)<', xml):
        try:
            out.setdefault(name, float(val) / 1e7)  # absolute rupees -> crore
        except ValueError:
            pass
    return out


async def xbrl_for(http: Http, sym: str, statement: str, qe: date) -> dict[str, float] | None:
    rows = (await http.get_json(XBRL_LIST.format(sym=sym), headers=REF)).get("data", [])
    want = qe.strftime("%d-%b-%Y").upper()
    for r in rows:
        if r.get("qe_Date") == want and r.get("consolidated", "").lower() == statement and r.get("xbrl"):
            xml = (await http.get_bytes(r["xbrl"], max_bytes=5 << 20, headers=REF)).decode("utf-8", "ignore")
            return facts(xml)
    return None


def shift(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    last = [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, last)


async def main(day: date) -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s", filename=str(Path(tempfile.gettempdir()) / "accuracy.log"))
    root = Path(__file__).resolve().parents[1]
    tmp = Path(tempfile.mkdtemp())
    env = tmp / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=1\n")  # no LLM keys on purpose
    s = load_settings(root / "config.example.toml", env)
    http, store = Http(), Store(tmp / "a.db")
    app = App(s, store=store, http=http, telegram=Quiet())
    await app.scrips.refresh()
    states: dict[str, int] = {}
    for src in app.sources.values():
        for f in await src.fetch(day):
            st = await app.pipeline.process(f)
            states[st] = states.get(st, 0) + 1
    print("states:", states)

    events = [json.loads(r[0]) for r in store._db.execute("select json from events")]
    numeric = [e for e in events if e.get("rows")]
    print(f"alerts: {len(events)}  with numbers: {len(numeric)}  headline-only: {len(events) - len(numeric)}\n")
    checked = wrong = 0
    for e in numeric:
        qe = date.fromisoformat(e["period_end"])
        sym = e["symbol"]
        line = f"{sym:12} {e['statement'][:4]} {e['source']:6}"
        for col, qd in enumerate((qe, shift(qe, 3), shift(qe, 12))):
            x = await xbrl_for(http, sym, e["statement"], qd) if sym else None
            if x is None:
                line += f" | col{col}: no XBRL"
                continue
            for key, names in FIELDS.items():
                ours = (e["rows"].get(key) or [None, None, None])[col]
                truth = next((x[n] for n in names if n in x), None)
                if ours is None or truth is None:
                    continue
                checked += 1
                if abs(ours - truth) > max(0.02, abs(truth) * 0.0005):
                    wrong += 1
                    line += f" | ✗ {key}@{col} ours {ours:.2f} xbrl {truth:.2f}"
        print(line)
    print(f"\nnumbers checked against XBRL: {checked}   mismatches: {wrong}")
    await http.close()


if __name__ == "__main__":
    asyncio.run(main(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()))
