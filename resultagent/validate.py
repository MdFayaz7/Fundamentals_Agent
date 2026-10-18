from __future__ import annotations

from itertools import product

from resultagent.models import Cell, Extraction, Template, Validation
from resultagent.numparse import UNIT_DIVISORS

# A variant is ((key, coef), ...): the identity holds when sum(coef * value) ≈ 0.
# The term with coef -1 listed last is the identity's result (its value sets the tolerance).
Variant = tuple[tuple[str, float], ...]

_OPTIONAL = {"exceptional", "associates"}  # missing => 0


def _v(*terms: tuple[str, float]) -> Variant:
    return terms


def _c3() -> list[Variant]:
    return [
        _v(("pbt_before_exceptional", 1), ("associates", a), ("exceptional", s), ("pbt", -1))
        for a, s in product((0, 1), (1, -1))
    ]


def _direct_pbt() -> list[Variant]:
    """Total income − total expenses (± exceptional, + associates) ≈ PBT. Checks the middle of
    the statement when the 'profit before exceptional items' row is absent (common)."""
    return [
        _v(("total_income", 1), ("total_expenses", -1), ("associates", a), ("exceptional", s), ("pbt", -1))
        for a, s in product((0, 1), (1, -1))
    ]


def _c4() -> list[Variant]:
    return [_v(("pbt", 1), ("tax", -1), ("associates", a), ("pat", -1)) for a in (0, 1)]


_IDENTITIES: dict[Template, list[tuple[str, list[Variant]]]] = {
    Template.CORPORATE: [
        ("C1", [_v(("revenue", 1), ("other_income", 1), ("total_income", -1))]),
        ("C2", [
            _v(("total_income", 1), ("total_expenses", -1), ("associates", a),
               ("pbt_before_exceptional", -1))
            for a in (0, 1)
        ]),
        ("C3", _c3()),
        ("C5", _direct_pbt()),
        ("C4", _c4()),
    ],
    Template.BANK: [
        ("B1", [_v(("interest_earned", 1), ("other_income", 1), ("total_income", -1))]),
        ("B2", [_v(("total_income", 1), ("interest_expended", -1), ("opex", -1),
                   ("ppop", -1))]),
        ("B3", [_v(("ppop", 1), ("provisions", -1), ("exceptional", s), ("pbt", -1))
                for s in (1, -1)]),
        # Banks that print no operating-profit row.
        ("B5", [_v(("total_income", 1), ("interest_expended", -1), ("opex", -1), ("provisions", -1),
                   ("exceptional", s), ("pbt", -1)) for s in (1, -1)]),
        ("B4", [_v(("pbt", 1), ("tax", -1), ("pat", -1))]),
    ],
    Template.LENDER: [
        ("L1", [_v(("revenue", 1), ("other_income", 1), ("total_income", -1))]),
        ("L2", [_v(("total_income", 1), ("total_expenses", -1),
                   ("pbt_before_exceptional", -1))]),
        ("L3", _c3()),
        ("L5", _direct_pbt()),
        ("L4", _c4()),
    ],
}

# Fallback identities only run when the row they stand in for is missing in that column,
# so they never double-count evidence for a repair.
_FALLBACK_FOR = {"C5": "pbt_before_exceptional", "L5": "pbt_before_exceptional", "B5": "ppop"}

_DECIMAL_LOSS_KEYS = ("finance_cost", "depreciation")
_NEVER_REPAIR = {"pat"}


def _mandatory(variants: list[Variant], key: str) -> bool:
    """True when key carries a non-zero coefficient in every variant. Terms that some
    variants leave out (e.g. associates) can be absorbed by those variants, so an
    identity passing never independently confirms such a term."""
    return all(dict(var).get(key) for var in variants)


def _keys(variants: list[Variant]) -> list[str]:
    out: list[str] = []
    for var in variants:
        for key, coef in var:
            if coef and key not in out:
                out.append(key)
    return out


class _Checker:
    def __init__(self, ex: Extraction):
        self.ex = ex
        self.min_tol = 0.05 / UNIT_DIVISORS[ex.unit]
        self.identities = _IDENTITIES[ex.template]

    def _value(self, key: str, col: int) -> float | None:
        v = self.ex.val(key, col)
        if v is None and key in _OPTIONAL:
            return 0.0
        return v

    def _variant_ok(self, var: Variant, col: int) -> bool:
        total = 0.0
        for key, coef in var:
            if coef:
                total += coef * self._value(key, col)
        result = self._value(var[-1][0], col)
        return abs(total) <= max(self.min_tol, 0.0001 * abs(result))

    def evaluable(self, variants: list[Variant], col: int) -> bool:
        return all(self._value(k, col) is not None for k in _keys(variants))

    def status(self, variants: list[Variant], col: int) -> bool | None:
        """True pass, False fail, None skipped (a mandatory term is missing)."""
        if not self.evaluable(variants, col):
            return None
        return any(self._variant_ok(var, col) for var in variants)

    def active(self, col: int) -> list[tuple[str, list[Variant]]]:
        return [(n, vs) for n, vs in self.identities
                if n not in _FALLBACK_FOR or self.ex.val(_FALLBACK_FOR[n], col) is None]

    def statuses(self, col: int) -> dict[str, bool | None]:
        act = {n for n, _ in self.active(col)}
        return {name: self.status(vs, col) if name in act else None for name, vs in self.identities}

    def solve(self, var: Variant, key: str, col: int) -> float:
        coef = dict(var)[key]
        residual = sum(c * self._value(k, col) for k, c in var if c)
        return self._value(key, col) - residual / coef


def _repair_decimal_loss(ex: Extraction, repaired: list[str]) -> None:
    for key in _DECIMAL_LOSS_KEYS:
        cells = ex.rows.get(key)
        if not cells:
            continue
        for col, cell in enumerate(cells):
            if not cell.anomaly or cell.value is None:
                continue
            others = [
                c.value for i, c in enumerate(cells) if i != col and c.value is not None
            ]
            fixed = cell.value / 100
            if others and all(o and 0.33 <= fixed / o <= 3 for o in others):
                cells[col] = Cell(fixed, anomaly=False)
                repaired.append(f"{key}@{col}")


def _repair_column(chk: _Checker, col: int, repaired: list[str]) -> None:
    ex = chk.ex
    before = chk.statuses(col)
    for name, variants in chk.active(col):
        if before[name] is not False:
            continue
        for key in _keys(variants):
            if key in _NEVER_REPAIR or ex.val(key, col) is None:
                continue
            cells = ex.rows[key]
            involved = [
                n for n, vs in chk.active(col)
                if _mandatory(vs, key) and chk.evaluable(vs, col)
            ]
            if not cells[col].anomaly and len(involved) < 2:
                continue
            original = cells[col]
            for var in variants:
                if not dict(var).get(key):
                    continue
                cells[col] = Cell(chk.solve(var, key, col), anomaly=False)
                after = chk.statuses(col)
                # The solved identity and every identity the cell takes part in must now
                # pass, and no identity that passed before may break.
                if after[name] and all(after[n] for n in involved) and all(
                    after[n] for n, s in before.items() if s
                ):
                    repaired.append(f"{key}@{col}")
                    return
                cells[col] = original


def _sanity_npa(ex: Extraction) -> None:
    g_cells, n_cells = ex.rows.get("gnpa_pct"), ex.rows.get("nnpa_pct")
    for col in range(3):
        g, n = ex.val("gnpa_pct", col), ex.val("nnpa_pct", col)
        if g is None and n is None:
            continue
        lo = 0.0 if n is None else n
        hi = 40.0 if g is None else g
        if not (0 <= lo <= hi <= 40):
            for cells in (g_cells, n_cells):
                if cells:
                    cells[col] = Cell(None)


def _sanity_eps(ex: Extraction) -> None:
    pats = [ex.val("pat", c) for c in (0, 2)]
    epss = [ex.val("eps", c) for c in (0, 2)]
    if None in pats or None in epss or 0 in epss:
        return
    shares0, shares2 = pats[0] / epss[0], pats[1] / epss[1]
    if abs(shares0 - shares2) > 0.25 * abs(shares2):
        ex.rows["eps"] = [Cell(None) for _ in ex.rows["eps"]]


def validate(ex: Extraction) -> Validation:
    chk = _Checker(ex)
    repaired: list[str] = []
    _repair_decimal_loss(ex, repaired)
    for col in range(3):
        _repair_column(chk, col, repaired)

    passed: list[str] = []
    failed: list[str] = []
    status: list[dict[str, bool | None]] = []
    for col in range(3):
        st = chk.statuses(col)
        status.append(st)
        for name, s in st.items():
            if s is True:
                passed.append(f"{name}@{col}")
            elif s is False:
                failed.append(f"{name}@{col}")

    names = [name for name, _ in chk.identities]
    first, middle, last = names[0], names[1:-1], names[-1]
    ok = bool(status[0][first] and status[0][last] and any(status[0][m] for m in middle))
    mandatory = {
        k for _, vs in chk.identities for k in _keys(vs) if k not in _OPTIONAL
    }
    for col in (1, 2):
        present = any(ex.val(k, col) is not None for k in mandatory)
        if present and not (status[col][first] and status[col][last]):
            ok = False
    # Any evaluated identity still failing after repair rejects the extraction.
    if failed:
        ok = False

    if ex.val("pat", 0) is None:
        ok = False
        failed.append("pat@0")
    _sanity_npa(ex)
    _sanity_eps(ex)
    return Validation(ok=ok, passed=passed, failed=failed, repaired=repaired)
