"""Exact parser for RedBox-style result headlines, e.g.

    HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)
    KROSS: Q2 EBITDA 306M RUPEES VS 147M (YOY); Q2 EBITDA MARGIN 13.02% VS 11.23% (YOY)

No AI: every number comes straight from the text. Amounts are converted to ₹ crore.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# rupee amount multipliers -> crore
_UNIT_CR = {"B": 100.0, "BN": 100.0, "BLN": 100.0, "M": 0.1, "MN": 0.1, "MLN": 0.1,
            "K": 0.0001, "CR": 1.0, "CRORE": 1.0, "L": 0.01, "LAKH": 0.01, "T": 100000.0}
_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_HEAD = re.compile(r"^\s*(?P<sym>[A-Z0-9][A-Z0-9&.\-]{0,24})\s*:\s*(?P<rest>.+)$")
_FACT = re.compile(
    rf"^(?P<per>Q[1-4]|H[12]|9M|FY\d{{0,4}})?\s*(?P<st>CONS(?:OLIDATED)?|SL|STANDALONE)?\s*"
    rf"(?P<item>[A-Z][A-Z /&\-()]*?)\s+(?P<val>{_NUM})\s*(?P<vu>%|BPS|[A-Z]+)?\s*"
    rf"(?:RUPEES|RS\.?|INR)?\s+VS\s+(?P<pword>LOSS|PROFIT)?\s*(?P<prev>{_NUM})\s*(?P<pu>%|BPS|[A-Z]+)?\s*"
    rf"(?:RUPEES|RS\.?|INR)?\s*\((?P<basis>YOY|QOQ|Y/Y|Q/Q)\)")
_METRICS = [
    (r"EBITDA MARGIN|OPERATING MARGIN", "ebitda_margin"),
    (r"EBITDA|OPERATING PROFIT", "ebitda"),
    (r"NET (?:PROFIT|LOSS)|PAT|PROFIT AFTER TAX|NET INCOME", "pat"),
    (r"NET INTEREST INCOME|\bNII\b", "nii"),
    (r"GROSS NPA|GNPA", "gnpa_pct"),
    (r"NET NPA|NNPA", "nnpa_pct"),
    (r"TOTAL INCOME", "total_income"),
    (r"REVENUE|SALES|INCOME FROM OPERATIONS", "revenue"),
]
LABELS = {"revenue": "Revenue", "ebitda": "EBITDA", "ebitda_margin": "EBITDA margin", "pat": "Net profit",
          "nii": "NII", "gnpa_pct": "Gross NPA", "nnpa_pct": "Net NPA", "total_income": "Total income"}


@dataclass(slots=True)
class Fact:
    symbol: str
    period: str          # Q1..Q4, H1, 9M, FY
    statement: str       # cons | sl | ""
    metric: str          # canonical key (see LABELS) or a slug of the raw item
    value: float         # ₹ crore, or % for margins / NPA
    prev: float
    basis: str           # yoy | qoq
    is_pct: bool

    @property
    def change(self) -> float | None:
        """% growth for amounts (None on a zero/negative base); bps for percentages."""
        if self.is_pct:
            return round((self.value - self.prev) * 100)
        if self.prev <= 0:
            return None
        return (self.value - self.prev) / self.prev * 100


def _metric(item: str) -> str:
    for pattern, key in _METRICS:
        if re.search(pattern, item):
            return key
    return re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_")


def _amount(num: str, unit: str | None) -> tuple[float, bool] | None:
    v = float(num.replace(",", ""))
    unit = (unit or "").upper()
    if unit in ("%", "BPS"):
        return (v / 100 if unit == "BPS" else v), True
    if unit in _UNIT_CR:
        return v * _UNIT_CR[unit], False
    if unit == "":
        return None  # unit carried over by the caller
    return None


def parse(text: str) -> list[Fact]:
    """All facts found in a message (one or more headline lines). Unknown lines are ignored."""
    facts: list[Fact] = []
    for line in text.upper().splitlines():
        head = _HEAD.match(line.strip())
        if not head:
            continue
        sym, period, statement = head["sym"], "", ""
        for part in head["rest"].split(";"):
            m = _FACT.match(part.strip())
            if not m:
                continue
            period = m["per"] or period
            st = m["st"] or ""
            statement = "cons" if st.startswith("CONS") else "sl" if st in ("SL", "STANDALONE") else statement
            vu, pu = m["vu"], m["pu"] or m["vu"]   # "6.3B RUPEES VS 1.9B" / "13.02% VS 11.23%"
            val, prev = _amount(m["val"], vu or pu), _amount(m["prev"], pu)
            if val is None or prev is None:
                continue
            (v, is_pct), (p, _) = val, prev
            item = m["item"].strip()
            if "LOSS" in item and not is_pct:
                v = -abs(v)
            if m["pword"] == "LOSS":
                p = -abs(p)
            facts.append(Fact(sym, period or "Q?", statement, _metric(item), v, p,
                              "qoq" if m["basis"].startswith("Q") else "yoy", is_pct))
    # A post states CONS/SL once; it applies to that company's other lines in the same message.
    known = {f.symbol: f.statement for f in facts if f.statement}
    for f in facts:
        f.statement = f.statement or known.get(f.symbol, "")
    return facts


def fmt_value(f: Fact) -> str:
    if f.is_pct:
        return f"{f.value:g}%"
    v = f.value
    return f"₹{v:,.0f} Cr" if abs(v) >= 100 else f"₹{v:,.1f} Cr"


def fmt_change(f: Fact) -> str:
    c = f.change
    if c is None:
        return "loss→profit" if f.prev < 0 < f.value else "n/a"
    return f"{c:+.0f} bps" if f.is_pct else f"{c:+.0f}%"
