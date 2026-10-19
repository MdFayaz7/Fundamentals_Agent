from resultagent.redbox import parse
from resultagent.scoring import score

HFCL = ("HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)\n"
        "HFCL: Q2 EBITDA 6.3B RUPEES VS 1.9B (YOY); Q2 EBITDA MARGIN 24.8% VS 18.4% (YOY)\n"
        "HFCL: Q2 REVENUE 25.4B RUPEES VS 10.4B (YOY)")


def test_strong_broad_based_result_scores_high():
    s, notes = score(parse(HFCL))
    assert s >= 90


def test_weak_and_loss_making_scores_low():
    s, notes = score(parse("ABC: Q2 NET LOSS 500M RUPEES VS PROFIT 200M (YOY)\n"
                           "ABC: Q2 REVENUE 1B RUPEES VS 1.2B (YOY); Q2 EBITDA MARGIN 5% VS 9% (YOY)"))
    assert s < 30 and "loss-making quarter" in notes and "margins contracting" in notes


def test_small_base_and_one_off_flags():
    _, notes = score(parse("XYZ: Q2 NET PROFIT 40M RUPEES VS 10M (YOY)\nXYZ: Q2 REVENUE 1.1B RUPEES VS 1B (YOY)"))
    assert any("small profit base" in n for n in notes) and any("one-offs" in n for n in notes)
