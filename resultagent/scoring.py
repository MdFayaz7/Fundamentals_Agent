"""Deterministic 0-100 quality score for one company's quarter (from saved YoY facts), so rankings are
anchored in arithmetic rather than the model's mood."""
from __future__ import annotations

from .redbox import Fact


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _band(x: float | None, lo: float, hi: float, weight: float) -> float:
    """Linear 0..weight between lo and hi; a missing value scores neutral (half)."""
    if x is None:
        return weight / 2
    return (_clamp(x, lo, hi) - lo) / (hi - lo) * weight


def score(facts: list[Fact]) -> tuple[int, list[str]]:
    """(score 0-100, short notes). Uses YoY facts; banks use NII in place of revenue."""
    yoy = {f.metric: f for f in facts if f.basis == "yoy"}
    g = {k: f.change for k, f in yoy.items()}
    rev = g.get("revenue", g.get("nii", g.get("total_income")))
    pat, ebitda, margin = g.get("pat"), g.get("ebitda"), g.get("ebitda_margin")
    s = (_band(rev, -20, 60, 25) + _band(pat, -50, 120, 30) + _band(ebitda, -30, 100, 20)
         + _band(margin, -300, 300, 15))
    notes = []
    positives = sum(1 for x in (rev, ebitda, pat) if x is not None and x > 0)
    s += 10 if positives == 3 else 5 if positives == 2 else 0
    p = yoy.get("pat")
    if p and p.value < 0:
        s -= 15
        notes.append("loss-making quarter")
    elif p and p.prev < 0 < p.value:
        s += 5
        notes.append("turned profitable")
    if p and 0 < p.prev < 5:
        notes.append("small profit base (₹<5 Cr) — % growth exaggerated")
    if pat is not None and rev is not None and rev > 0 and pat > 3 * rev + 50:
        notes.append("profit growing far faster than revenue — check for one-offs/other income")
    if margin is not None and margin <= -100:
        notes.append("margins contracting")
    return int(round(_clamp(s, 0, 100))), notes
