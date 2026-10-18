from datetime import date

from pytest import approx

from resultagent.analyse import analyse, growth
from resultagent.extract import extract_rows
from resultagent.models import Cell, Extraction, Template


def _m(a, name):
    return next(m for m in a.metrics if m.name == name)


def _corp(pat):
    rows = {
        "revenue": [100.0, 100.0, 100.0],
        "total_expenses": [90.0, 90.0, 90.0],
        "finance_cost": [1.0, 1.0, 1.0],
        "depreciation": [2.0, 2.0, 2.0],
        "pbt": [10.0, 10.0, 10.0],
        "pat": pat,
    }
    return Extraction(
        Template.CORPORATE, "standalone", date(2026, 9, 30),
        {k: [Cell(v) for v in vs] for k, vs in rows.items()}, "rules", "crore",
    )


def test_growth_negative_base_is_none():
    assert growth(10, -5) is None
    assert growth(10, 0) is None
    assert growth(12, 10) == approx(20.0)


def test_rossari():
    ex = extract_rows(open("tests/fixtures/pages/rossari_cons.txt").read())
    a = analyse(ex)
    rev = _m(a, "Revenue")
    assert rev.yoy == approx(19.02, abs=0.05)
    assert rev.qoq == approx(0.05, abs=0.05)
    assert _m(a, "EBITDA").value == approx(86.974, abs=0.01)
    mg = _m(a, "EBITDA margin")
    assert mg.value == approx(12.47, abs=0.02)
    assert mg.yoy == approx(20, abs=3)
    assert _m(a, "PAT").yoy == approx(5.51, abs=0.05)
    assert a.score == 6
    assert a.verdict == "GOOD"


def test_hdfc_bank(hdfc_truth):
    a = analyse(hdfc_truth)
    nii = _m(a, "NII")
    assert nii.value == approx(34619.18, abs=0.01)
    assert nii.yoy == approx(9.72, abs=0.05)
    assert nii.qoq == approx(3.24, abs=0.05)
    assert _m(a, "PAT").yoy == approx(4.66, abs=0.05)
    assert _m(a, "GNPA").yoy == approx(-0.13, abs=0.001)
    assert any(f.positive and f.text.startswith("Bad loans (GNPA) improved to 1.11%")
               for f in a.flags)
    assert a.score == 6
    assert a.verdict == "GOOD"


def test_turnaround():
    a = analyse(_corp([10, 5, -3]))
    assert any(f.text == "Turned from loss to profit vs last year" for f in a.flags)
    assert _m(a, "PAT").yoy is None
    assert a.score == analyse(_corp([10, 5, -3]), {"turnaround": 0}).score + 2


def test_to_loss():
    a = analyse(_corp([-4, 5, 6]))
    assert any(f.text == "Slipped into a loss vs last year" for f in a.flags)
    assert a.verdict in ("WEAK", "POOR")


def test_weights_override():
    ex = _corp([10, 5, -3])
    assert analyse(ex).score != analyse(ex, {"turnaround": 0}).score
