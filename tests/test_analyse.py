from datetime import date

from pytest import approx

from resultagent.analyse import analyse, growth
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
    assert growth(-4, 6) is None


def make_ex(template, statement, **rows):
    from resultagent.models import Cell, Extraction, Template
    return Extraction(Template(template), statement, None,
                      {k: [Cell(v) for v in vals] for k, vals in rows.items()}, "xbrl", "crore")


def test_rossari():
    ex = make_ex("corporate", "consolidated", revenue=[697.560, 697.200, 586.107],
                 other_income=[0.599, 3.187, 1.604], total_income=[698.159, 700.387, 587.711],
                 total_expenses=[649.359, 653.172, 538.507], finance_cost=[8.677, 10.977, 6.091],
                 depreciation=[30.096, 25.615, 18.233], pbt=[49.269, 47.672, 49.672],
                 tax=[10.362, 12.577, 12.796], pat=[38.907, 35.095, 36.876], eps=[7.02, 6.34, 6.66])
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
    assert _m(a, "PAT").yoy is None
    assert not any(f.text.startswith("Profit fell") for f in a.flags)


def test_missing_depreciation_no_margin_signal():
    ex = _corp([10, 10, 10])
    ex.rows["depreciation"] = [Cell(10.0), Cell(10.0), Cell(None)]
    a = analyse(ex)
    assert _m(a, "EBITDA").yoy is None
    assert _m(a, "EBITDA margin").yoy is None
    assert not any(f.text.startswith("Operating margin") for f in a.flags)
    assert a.score == 5


def test_thin_data_flag():
    ex = Extraction(Template.CORPORATE, "standalone", None,
                    {"revenue": [Cell(100.0)], "pat": [Cell(10.0)]}, "rules", "crore")
    a = analyse(ex)
    assert any(f.text.startswith("Not enough past data") for f in a.flags)


def test_lender():
    ex = make_ex("lender", "standalone", interest_income=[3051.74, 2856.07, 2614.44],
                 revenue=[3250.57, 3063.02, 2748.04], finance_cost=[1996.65, 1888.49, 1657.96],
                 impairment=[34.78, 16.10, 42.85], pat=[756.97, 715.28, 642.96])
    a = analyse(ex)
    assert _m(a, "NII").value == approx(1055.09, abs=0.01)


def test_provisions_outpace():
    rows = {
        "interest_earned": [200.0, 200.0, 200.0], "interest_expended": [100.0, 100.0, 100.0],
        "ppop": [50.0, 50.0, 50.0], "provisions": [90.0, 50.0, 50.0],
        "pat": [10.0, 10.0, 10.0],
    }
    ex = Extraction(Template.BANK, "standalone", None,
                    {k: [Cell(x) for x in vs] for k, vs in rows.items()}, "rules", "crore")
    a = analyse(ex)
    assert any(f.text.startswith("Bad-loan provisions are growing faster") for f in a.flags)
    assert a.score == analyse(ex, {"provisions_outpace": 0}).score - 1


def test_weights_override():
    ex = _corp([10, 5, -3])
    assert analyse(ex).score != analyse(ex, {"turnaround": 0}).score
