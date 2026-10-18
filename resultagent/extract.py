from __future__ import annotations

import re

from resultagent.models import Cell, Extraction, Template
from resultagent.numparse import detect_period_end, detect_unit_divisor, tokenize_numbers

ROW_KEYS: dict[Template, tuple[str, ...]] = {
    Template.CORPORATE: (
        "revenue", "other_income", "total_income", "total_expenses", "finance_cost",
        "depreciation", "pbt_before_exceptional", "associates", "exceptional", "pbt",
        "tax", "pat", "pat_owners", "eps",
    ),
    Template.BANK: (
        "interest_earned", "other_income", "total_income", "interest_expended", "opex",
        "ppop", "provisions", "exceptional", "pbt", "tax", "pat", "eps", "gnpa_pct",
        "nnpa_pct",
    ),
    Template.LENDER: (
        "interest_income", "revenue", "other_income", "total_income", "finance_cost",
        "impairment", "total_expenses", "pbt_before_exceptional", "exceptional", "pbt",
        "tax", "pat", "eps",
    ),
}

_UNSCALED = {"eps", "gnpa_pct", "nnpa_pct"}


def _rx(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# A label not preceded by "before" on its line ("Profit before share of profit of
# associates / exceptional items ..." is a different row).
def _not_after_before(label: str) -> str:
    return rf"^(?:(?!before).)*?{label}"


_PBT_BEFORE_EXC = r"profit.{0,40}before.{0,60}exceptional"
_EXCEPTIONAL = _not_after_before(r"exceptional\s*[il]tems")
_PBT = r"profit.{0,40}before\s*tax"
_TOTAL_TAX = r"total\s*tax\s*expense"
_TAX = r"^(?!.*(?:discontinued|net of tax|relating)).*?tax\s*expense"
_EPS = (r"\bbasic\b", r"earnings\s*per\s*(?:equity\s*)?share")

_LABELS: dict[Template, dict[str, list[re.Pattern]]] = {
    Template.CORPORATE: {
        "revenue": _rx(r"revenue\s*from\s*operations"),
        "other_income": _rx(r"other\s*income"),
        "total_income": _rx(r"total\s*income"),
        "total_expenses": _rx(r"total\s*expenses"),
        "finance_cost": _rx(r"finance\s*cost"),
        "depreciation": _rx(r"depreciation"),
        "pbt_before_exceptional": _rx(_PBT_BEFORE_EXC),
        "associates": _rx(
            _not_after_before(r"share of (?:net )?profit.{0,30}(?:associate|joint venture)")
        ),
        "exceptional": _rx(_EXCEPTIONAL),
        "pbt": _rx(_PBT),
        "tax": _rx(_TOTAL_TAX, _TAX),
        "pat_owners": _rx(r"owners of the (?:company|parent)"),
        "eps": _rx(*_EPS),
    },
    Template.BANK: {
        "interest_earned": _rx(r"interest\s*earned"),
        "other_income": _rx(r"other\s*income"),
        "total_income": _rx(r"total\s*income"),
        "interest_expended": _rx(r"interest\s*expend"),
        "opex": _rx(r"operating\s*expenses"),
        "ppop": _rx(r"operating\s*profit\s*before\s*provisions"),
        "provisions": _rx(r"provisions\s*\(\s*other\s*than\s*tax\s*\)"),
        "exceptional": _rx(_EXCEPTIONAL),
        "pbt": _rx(_PBT),
        "tax": _rx(_TOTAL_TAX, _TAX),
        "pat": _rx(r"net\s*profit.{0,40}for\s*the\s*period", r"net\s*profit.{0,40}after\s*tax"),
        "eps": _rx(*_EPS),
        "gnpa_pct": _rx(r"%\s*of\s*gross\s*npa"),
        "nnpa_pct": _rx(r"%\s*of\s*net\s*npa"),
    },
    Template.LENDER: {
        "interest_income": _rx(r"interest\s*income"),
        "revenue": _rx(r"total\s*revenue\s*from\s*operations"),
        "other_income": _rx(r"other\s*income"),
        "total_income": _rx(r"total\s*income"),
        "finance_cost": _rx(r"finance\s*cost"),
        "impairment": _rx(r"impairment\s*on\s*financial\s*instruments"),
        "total_expenses": _rx(r"total\s*expenses"),
        "pbt_before_exceptional": _rx(_PBT_BEFORE_EXC),
        "exceptional": _rx(_EXCEPTIONAL),
        "pbt": _rx(_PBT),
        "tax": _rx(_TOTAL_TAX, _TAX),
        "pat": _rx(r"profit\s*after\s*tax", r"profit\s*for\s*the\s*period"),
        "eps": _rx(*_EPS),
    },
}

# CORPORATE pat: consolidated net profit after minority/associates first, then plain PAT.
_PAT_AFTER_MINORITY = _rx(r"net profit.{0,40}after.{0,40}(?:minority|non.controlling|associates)")
_PAT_PLAIN = _rx(r"profit (?:after tax|for the period|/ ?\(loss\) for the period)")
_CONTINUING = re.compile(r"continuing operations", re.IGNORECASE)

_TAX_PARTS = _rx(r"current\s*tax", r"deferred\s*tax", r"earlier\s*years?")

# Chained formula references such as "(1)+(2)", "(3)-(6)", "(i)+(ii)", "(5) - (6)".
_FORMULA_CHAIN = re.compile(
    r"[\(\{\[]\w{1,4}[\)\}\]](?:\s*[+\-]\s*[\(\{\[]\w{1,4}[\)\}\]])+"
)
_UNIT_TOKENS = re.compile(r"\b(?:rs|inr|cr|crores?|lakhs?|mn|millions?)\b\.?", re.IGNORECASE)


def detect_template(page_text: str) -> Template | None:
    t = page_text.lower()
    if "interest earned" in t and re.search(r"interest\s*expend", t):
        return Template.BANK
    if "interest income" in t and (
        "impairment on financial instruments" in t or "finance cost" in t
    ):
        return Template.LENDER
    if "revenue from operations" in t or "total income" in t:
        return Template.CORPORATE
    return None


def detect_statement(page_text: str) -> str:
    return "consolidated" if "consolidated" in page_text[:800].lower() else "standalone"


def _cells(text: str) -> list[Cell]:
    return tokenize_numbers(_FORMULA_CHAIN.sub(" ", text))


def _is_number_line(line: str) -> bool:
    if re.search(r"[A-Za-z]", _UNIT_TOKENS.sub(" ", line)):
        return False
    return len(_cells(line)) >= 3


def _cells_at(lines: list[str], i: int, end: int) -> list[Cell]:
    """Cells after the label match (ending at `end`) on line i, topped up from
    up to the next 3 purely numeric lines when the label line has <3 cells."""
    cells = _cells(lines[i][end:])
    for nxt in lines[i + 1:i + 4]:
        if len(cells) >= 3:
            break
        if _is_number_line(nxt):
            cells.extend(_cells(nxt))
    return cells[:3]


def _matches(lines: list[str], pattern: re.Pattern):
    """Yield (line_index, line, cells) for each line matching pattern with >=1 cell."""
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if not m:
            continue
        cells = _cells_at(lines, i, m.end())
        if cells:
            yield i, line, cells


def _first(lines: list[str], patterns: list[re.Pattern]) -> list[Cell] | None:
    for pattern in patterns:
        for _, _, cells in _matches(lines, pattern):
            return cells
    return None


def _corporate_pat(lines: list[str]) -> list[Cell] | None:
    found = _first(lines, _PAT_AFTER_MINORITY)
    if found:
        return found
    hits = [(line, cells) for p in _PAT_PLAIN for _, line, cells in _matches(lines, p)]
    for k, (line, cells) in enumerate(hits):
        if _CONTINUING.search(line) and any(not _CONTINUING.search(h[0]) for h in hits[k + 1:]):
            continue
        return cells
    return None


def _pad(cells: list[Cell]) -> list[Cell]:
    return cells[:3] + [Cell(None) for _ in range(3 - len(cells[:3]))]


def _sum_tax_parts(lines: list[str]) -> list[Cell] | None:
    parts = [_first(lines, [p]) for p in _TAX_PARTS]
    parts = [_pad(p) for p in parts if p]
    if not parts:
        return None
    out = []
    for col in range(3):
        vals = [p[col].value for p in parts if p[col].value is not None]
        out.append(
            Cell(sum(vals) if vals else None, anomaly=any(p[col].anomaly for p in parts))
        )
    return out


def extract_rows(page_text: str) -> Extraction | None:
    template = detect_template(page_text)
    if template is None:
        return None
    lines = page_text.splitlines()
    unit, divisor = detect_unit_divisor(page_text)
    labels = _LABELS[template]
    rows: dict[str, list[Cell]] = {}
    for key in ROW_KEYS[template]:
        if template is Template.CORPORATE and key == "pat":
            cells = _corporate_pat(lines)
        else:
            cells = _first(lines, labels[key])
        if cells is None and key == "tax":
            cells = _sum_tax_parts(lines)
        if not cells:
            continue
        cells = _pad(cells)
        if key not in _UNSCALED:
            cells = [
                Cell(c.value / divisor if c.value is not None else None, c.anomaly)
                for c in cells
            ]
        rows[key] = cells
    return Extraction(
        template=template,
        statement=detect_statement(page_text),
        period_end=detect_period_end(page_text),
        rows=rows,
        source="rules",
        unit=unit,
    )
