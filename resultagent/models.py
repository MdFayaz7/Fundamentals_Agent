from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Template(StrEnum):
    CORPORATE = "corporate"
    BANK = "bank"
    LENDER = "lender"


class Confidence(StrEnum):
    VERIFIED = "VERIFIED"
    LLM_VERIFIED = "LLM_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    NO_TEXT = "NO_TEXT"


@dataclass(slots=True)
class Filing:
    exchange: str
    filing_id: str
    isin: str | None
    symbol: str
    name: str
    pdf_url: str
    disseminated_at: datetime
    subject: str
    scrip_code: str | None = None

    @property
    def key(self) -> str:
        # ISIN chars 0-8 = country + issuer + security type. NSE sometimes reports an older ISIN
        # than BSE for the same share (e.g. INE528G01019 vs INE528G01035), so dedupe on the prefix.
        return self.isin[:9] if self.isin else f"BSE:{self.scrip_code}"


@dataclass(slots=True)
class Cell:
    value: float | None
    anomaly: bool = False


@dataclass(slots=True)
class Extraction:
    template: Template
    statement: str  # standalone | consolidated
    period_end: date | None
    rows: dict[str, list[Cell]]  # each list has length 3
    source: str  # rules | llm_vision
    unit: str

    def val(self, key: str, col: int) -> float | None:
        cells = self.rows.get(key)
        if cells is None or col >= len(cells):
            return None
        return cells[col].value


@dataclass(slots=True)
class Validation:
    ok: bool
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Metric:
    name: str
    value: float | None
    yoy: float | None
    qoq: float | None
    kind: str = "cr"  # cr | pct | eps | margin


@dataclass(slots=True)
class Flag:
    positive: bool
    text: str


@dataclass(slots=True)
class Analysis:
    metrics: list[Metric]
    flags: list[Flag]
    score: int
    verdict: str
    summary: str
