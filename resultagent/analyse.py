from __future__ import annotations

from resultagent.models import Analysis, Extraction, Flag, Metric, Template

DEFAULT_WEIGHTS: dict[str, float] = {
    "rev_yoy_gt20": 2, "rev_yoy_gt10": 1, "rev_yoy_lt0": -1, "rev_yoy_ltm10": -2,
    "pat_yoy_gt25": 2, "pat_yoy_gt10": 1, "pat_yoy_lt0": -1, "pat_yoy_ltm20": -2,
    "margin_up100": 1, "margin_down100": -1,
    "turnaround": 2, "to_loss": -3,
    "other_income_heavy": -1, "exceptional_heavy": -1,
    "provisions_outpace": -1,
    "gnpa_down": 1, "gnpa_up": -1,
    "qoq_rev_drop": -1,
}

_SUMMARY = {
    "STRONG": "Strong quarter: both sales and profit are growing well.",
    "GOOD": "Good quarter: growth is healthy, with minor watch-points.",
    "MIXED": "Mixed quarter: some numbers improved, others weakened.",
    "WEAK": "Weak quarter: growth is slowing or quality is poor.",
    "POOR": "Poor quarter: sales and/or profit fell sharply.",
}


def growth(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base <= 0:
        return None
    return (cur - base) / base * 100


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _series(fn) -> list[float | None]:
    return [fn(c) for c in range(3)]


def _metric(name: str, vals: list[float | None], kind: str = "cr") -> Metric:
    if kind == "margin":
        yoy, qoq = _sub(vals[0], vals[2]), _sub(vals[0], vals[1])
        yoy = None if yoy is None else yoy * 100
        qoq = None if qoq is None else qoq * 100
    elif kind == "pct":
        yoy, qoq = _sub(vals[0], vals[2]), _sub(vals[0], vals[1])
    else:
        yoy, qoq = growth(vals[0], vals[2]), growth(vals[0], vals[1])
    return Metric(name, vals[0], yoy, qoq, kind)


def _verdict(score: int) -> str:
    if score >= 8:
        return "STRONG"
    if score >= 6:
        return "GOOD"
    if score == 5:
        return "MIXED"
    if score >= 3:
        return "WEAK"
    return "POOR"


def analyse(ex: Extraction, weights: dict[str, float] | None = None) -> Analysis:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    v = ex.val
    t = ex.template

    def diff(a: str, b: str):
        return _series(lambda c: _sub(v(a, c), v(b, c)))

    metrics: list[Metric] = []
    ppop_like: Metric | None = None
    margin: Metric | None = None
    if t is Template.CORPORATE:
        ebitda = _series(lambda c: (
            None if v("revenue", c) is None or v("total_expenses", c) is None
            else v("revenue", c) - (v("total_expenses", c) - (v("finance_cost", c) or 0)
                                    - (v("depreciation", c) or 0))))
        mg = _series(lambda c: (
            ebitda[c] / v("revenue", c) * 100
            if ebitda[c] is not None and v("revenue", c) else None))
        pat_key = "pat_owners" if v("pat_owners", 0) is not None else "pat"
        growth_m = _metric("Revenue", _series(lambda c: v("revenue", c)))
        margin = _metric("EBITDA margin", mg, "margin")
        pat_m = _metric("PAT", _series(lambda c: v(pat_key, c)))
        metrics = [growth_m, _metric("EBITDA", ebitda), margin, pat_m,
                   _metric("EPS", _series(lambda c: v("eps", c)), "eps")]
    elif t is Template.BANK:
        nii = diff("interest_earned", "interest_expended")
        growth_m = _metric("NII", nii)
        ppop_like = _metric("PPOP", _series(lambda c: v("ppop", c)))
        prov_m = _metric("Provisions", _series(lambda c: v("provisions", c)))
        pat_m = _metric("PAT", _series(lambda c: v("pat", c)))
        metrics = [
            growth_m, _metric("Other income", _series(lambda c: v("other_income", c))),
            ppop_like, prov_m, pat_m,
            _metric("GNPA", _series(lambda c: v("gnpa_pct", c)), "pct"),
            _metric("NNPA", _series(lambda c: v("nnpa_pct", c)), "pct"),
        ]
    else:
        growth_m = _metric("NII", diff("interest_income", "finance_cost"))
        ppop_like = growth_m
        prov_m = _metric("Impairment", _series(lambda c: v("impairment", c)))
        pat_m = _metric("PAT", _series(lambda c: v("pat", c)))
        metrics = [
            growth_m, _metric("Total revenue", _series(lambda c: v("revenue", c))),
            prov_m, pat_m, _metric("EPS", _series(lambda c: v("eps", c)), "eps"),
        ]

    score = 5.0
    flags: list[Flag] = []
    core = t is not Template.CORPORATE

    # growth (sales / core income)
    ry = growth_m.yoy
    if ry is not None:
        if ry > 20:
            score += w["rev_yoy_gt20"]
        elif ry > 10:
            score += w["rev_yoy_gt10"]
        elif ry < -10:
            score += w["rev_yoy_ltm10"]
        elif ry < 0:
            score += w["rev_yoy_lt0"]
        if ry > 15:
            flags.append(Flag(True, f"Sales grew {ry:.0f}% vs last year"))
        elif ry < 0:
            flags.append(Flag(False, f"Sales fell {abs(ry):.0f}% vs last year"))
    if growth_m.qoq is not None and growth_m.qoq < -10:
        score += w["qoq_rev_drop"]

    # profit
    py = pat_m.yoy
    if py is not None:
        if py > 25:
            score += w["pat_yoy_gt25"]
        elif py > 10:
            score += w["pat_yoy_gt10"]
        elif py < -20:
            score += w["pat_yoy_ltm20"]
        elif py < 0:
            score += w["pat_yoy_lt0"]
        if py > 15:
            flags.append(Flag(True, f"Profit grew {py:.0f}% vs last year"))
        elif py < 0:
            flags.append(Flag(False, f"Profit fell {abs(py):.0f}% vs last year"))

    # margin
    if margin is not None and margin.yoy is not None:
        bps = margin.yoy
        if bps > 100:
            score += w["margin_up100"]
            flags.append(Flag(True, f"Operating margin improved by {bps:.0f} bps — "
                                    "the core business is getting more profitable"))
        elif bps < -100:
            score += w["margin_down100"]
            flags.append(Flag(False, f"Operating margin shrank by {abs(bps):.0f} bps — "
                                     "costs are rising faster than sales"))

    # turnaround / loss
    p0, p2 = pat_m.value, _series(lambda c: v("pat_owners" if t is Template.CORPORATE
                                              and v("pat_owners", 0) is not None
                                              else "pat", c))[2]
    if p0 is not None and p2 is not None:
        if p2 <= 0 < p0:
            score += w["turnaround"]
            flags.append(Flag(True, "Turned from loss to profit vs last year"))
        elif p2 > 0 >= p0:
            score += w["to_loss"]
            flags.append(Flag(False, "Slipped into a loss vs last year"))

    pbt, oi, exc, tax = v("pbt", 0), v("other_income", 0), v("exceptional", 0), v("tax", 0)

    if t is not Template.BANK and oi is not None and pbt is not None and pbt > 0:
        share = oi / pbt * 100
        if share > 25:
            score += w["other_income_heavy"]
            flags.append(Flag(False, f"Other income is {share:.0f}% of profit — part of "
                                     "the profit is not from the core business"))
    if exc is not None and pbt is not None and abs(exc) > 0.10 * abs(pbt):
        score += w["exceptional_heavy"]
        flags.append(Flag(False, f"One-time (exceptional) items of ₹{abs(exc):.0f} Cr "
                                 "affect profit — check before trusting the growth"))

    if core and prov_m.yoy is not None and ppop_like is not None and ppop_like.yoy is not None:
        if prov_m.yoy - ppop_like.yoy > 20:
            score += w["provisions_outpace"]
            flags.append(Flag(False, "Bad-loan provisions are growing faster than "
                                     "operating profit"))

    if t is Template.BANK:
        gn = metrics[5]
        if gn.qoq is not None and gn.value is not None:
            if gn.qoq <= -0.05:
                score += w["gnpa_down"]
                flags.append(Flag(True, f"Bad loans (GNPA) improved to {gn.value:.2f}%"))
            elif gn.qoq >= 0.05:
                score += w["gnpa_up"]
                flags.append(Flag(False, f"Bad loans (GNPA) worsened to {gn.value:.2f}%"))

    if tax is not None and pbt is not None and pbt > 0:
        rate = tax / pbt * 100
        if rate < 15 or rate > 35:
            flags.append(Flag(False, f"Tax rate looks unusual ({rate:.0f}%) — "
                                     "profit may not repeat"))

    final = max(1, min(10, round(score)))
    verdict = _verdict(final)
    summary = _SUMMARY[verdict]
    if core:
        summary = summary.replace("sales", "core income")
    return Analysis(metrics, flags, final, verdict, summary)
