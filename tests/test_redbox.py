import pytest

from resultagent.redbox import fmt_change, parse

HFCL = """HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)

HFCL: Q2 EBITDA 6.3B RUPEES VS 1.9B (YOY); Q2 EBITDA MARGIN 24.8% VS 18.4% (YOY)

HFCL: Q2 REVENUE 25.4B RUPEES VS 10.4B (YOY)"""

KROSS = """KROSS: Q2 SL NET PROFIT 197M RUPEES VS 81M (YOY)
KROSS: Q2 REVENUE 2.35B RUPEES VS 1.3B (YOY)
KROSS: Q2 EBITDA 306M RUPEES VS 147M (YOY); Q2 EBITDA MARGIN 13.02% VS 11.23% (YOY)"""


def by_metric(facts):
    return {f.metric: f for f in facts}


def test_hfcl():
    f = by_metric(parse(HFCL))
    assert set(f) == {"pat", "ebitda", "ebitda_margin", "revenue"}
    assert f["pat"].value == pytest.approx(360) and f["pat"].prev == pytest.approx(67.8)
    assert f["pat"].statement == "cons" and f["pat"].period == "Q2" and f["pat"].basis == "yoy"
    assert f["ebitda"].value == pytest.approx(630) and f["revenue"].prev == pytest.approx(1040)
    assert f["ebitda_margin"].is_pct and f["ebitda_margin"].change == 640
    assert fmt_change(f["pat"]) == "+431%"
    assert all(x.symbol == "HFCL" for x in f.values())


def test_kross_standalone_and_margin_on_same_line():
    f = by_metric(parse(KROSS))
    assert f["pat"].statement == "sl" and f["pat"].value == pytest.approx(19.7)
    assert f["revenue"].value == pytest.approx(235) and f["revenue"].prev == pytest.approx(130)
    assert f["ebitda_margin"].value == 13.02 and f["ebitda_margin"].change == 179


def test_loss_and_qoq():
    f = parse("XYZ: Q2 CONS NET LOSS 1.2B RUPEES VS PROFIT 300M (YOY)\n"
              "ABC: Q2 NII 45.6B RUPEES VS 41B (QOQ); Q2 GROSS NPA 1.11% VS 1.17% (QOQ)")
    loss = f[0]
    assert loss.metric == "pat" and loss.value == pytest.approx(-120) and loss.prev == pytest.approx(30)
    assert fmt_change(loss) == "n/a" or loss.change is not None
    nii, npa = f[1], f[2]
    assert nii.metric == "nii" and nii.basis == "qoq" and nii.value == pytest.approx(4560)
    assert npa.metric == "gnpa_pct" and npa.change == -6


def test_non_headline_text_ignored():
    assert parse("Markets closed higher today on FII buying.") == []
    assert parse("Which is the best result?") == []
