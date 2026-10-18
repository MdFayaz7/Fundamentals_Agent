"""Quarter helpers."""
from __future__ import annotations

from datetime import date


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
