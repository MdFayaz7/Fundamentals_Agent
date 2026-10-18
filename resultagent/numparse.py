from __future__ import annotations

import re
from datetime import date

from resultagent.models import Cell

UNIT_DIVISORS: dict[str, float] = {
    "crore": 1.0,
    "lakh": 100.0,
    "million": 10.0,
    "thousand": 10000.0,
    "rupees": 1e7,
}

_OPEN = "({["
_CLOSE = ")}]"
_BRACKET_GROUP = re.compile(r"[\(\{\[][^\(\)\{\}\[\]]*[\)\}\]]")
_REFER_NOTE = re.compile(r"\(refer note[^)]*\)", re.IGNORECASE)
_STRAY = re.compile("[|•'\"*’‘°]")
_DASHES = {"-", "–", "—"}
_BRACKET_CHARS = "(){}[]"


def _drop_formula(m: re.Match) -> str:
    g = m.group(0)
    if ("+" in g or "-" in g) and "." not in g:
        return " "
    return g


def _parse_word(word: str) -> tuple[Cell, bool, bool] | None:
    """Return (cell, is_integer, has_two_decimals) or None if not a token."""
    core = word.strip(_BRACKET_CHARS)
    if core in _DASHES:
        return Cell(0.0), False, False
    if not re.search(r"\d", word):
        return None
    negative = any(ch in word for ch in "(){}")
    s = word
    for ch in _BRACKET_CHARS:
        s = s.replace(ch, "")
    s = s.replace("%", "").strip(".,")
    if re.search(r"[A-Za-z]", s):
        return Cell(None, anomaly=True), False, False
    anomaly = False
    if ".." in s:
        s = re.sub(r"\.{2,}", ".", s)
        anomaly = True
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if s.count(",") == 1 and len(tail) == 2:
            s = f"{head}.{tail}"
        else:
            s = s.replace(",", "")
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
        anomaly = True
    if not re.fullmatch(r"\d+(\.\d*)?|\.\d+", s):
        return Cell(None, anomaly=True), False, False
    decimals = len(s.split(".")[1]) if "." in s else 0
    if decimals > 2:
        anomaly = True
    value = float(s)
    if negative:
        value = -value
    return Cell(value, anomaly), "." not in s, decimals == 2


def tokenize_numbers(s: str) -> list[Cell]:
    s = _REFER_NOTE.sub(" ", s)
    s = _BRACKET_GROUP.sub(_drop_formula, s)
    s = _STRAY.sub("", s)
    parsed: list[tuple[Cell, bool, bool]] = []
    for word in s.split():
        tok = _parse_word(word)
        if tok is None:
            if parsed and re.search(r"[A-Za-z]{4,}", word):
                break
            continue
        parsed.append(tok)
    has_two_dec = any(p[2] for p in parsed)
    out: list[Cell] = []
    for cell, is_int, _ in parsed:
        if has_two_dec and is_int and cell.value is not None:
            if abs(cell.value) < 100:
                continue
            cell.anomaly = True
        out.append(cell)
    return out


_UNIT_PATTERNS = [
    ("million", re.compile(r"millions?", re.I)),
    ("lakh", re.compile(r"lakhs?|lacs?", re.I)),
    ("crore", re.compile(r"crores?", re.I)),
    ("thousand", re.compile(r"thousands?", re.I)),
    ("rupees", re.compile(r"\bin\s+rupees\b", re.I)),
]


def detect_unit_divisor(page_text: str) -> tuple[str, float]:
    best: tuple[int, str] | None = None
    for name, pat in _UNIT_PATTERNS:
        m = pat.search(page_text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best is None:
        return "crore", UNIT_DIVISORS["crore"]
    return best[1], UNIT_DIVISORS[best[1]]


_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_FORMS = [
    # September 30, 2026
    (re.compile(rf"({_MON})\s*(\d{{1,2}})\s*,?\s*(\d{{4}})", re.I), "mdy"),
    # 30th September, 2026 / 30-Sep-2026
    (re.compile(rf"(\d{{1,2}})\s*(?:st|nd|rd|th)?[\s\-]*({_MON})\s*,?[\s\-]*(\d{{4}})", re.I), "dmy"),
    # 30.09.2026 / 30-09-2026
    (re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})"), "num"),
]
_PERIOD = re.compile(r"quarter[^\n]{0,60}?ended[^\n]{0,10}?(?=\S)", re.I)
_QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}


def _try_date(text: str) -> date | None:
    for pat, kind in _DATE_FORMS:
        m = pat.match(text)
        if not m:
            continue
        try:
            if kind == "mdy":
                mon, day, year = _MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
            elif kind == "dmy":
                day, mon, year = int(m.group(1)), _MONTHS[m.group(2)[:3].lower()], int(m.group(3))
            else:
                day, mon, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            d = date(year, mon, day)
        except (ValueError, KeyError):
            continue
        if (d.month, d.day) in _QUARTER_ENDS:
            return d
    return None


def detect_period_end(page_text: str) -> date | None:
    for m in _PERIOD.finditer(page_text):
        d = _try_date(page_text[m.end():m.end() + 40])
        if d:
            return d
    return None


def fallback_period_end(filed: date) -> date:
    for y in (filed.year, filed.year - 1):
        for mon, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            d = date(y, mon, day)
            if d < filed:
                return d
    raise ValueError(filed)  # unreachable


def fy_label(period_end: date) -> str:
    q = {6: 1, 9: 2, 12: 3, 3: 4}[period_end.month]
    fy = period_end.year + (1 if period_end.month > 3 else 0)
    return f"Q{q} FY{fy % 100:02d}"
