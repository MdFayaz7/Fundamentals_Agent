from pathlib import Path

import pytest

from resultagent.extract import extract_rows
from resultagent.models import Cell, Extraction, Template
from resultagent.validate import validate

FIX = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def corporate(**rows: list[float]) -> Extraction:
    return Extraction(
        template=Template.CORPORATE,
        statement="standalone",
        period_end=None,
        rows={k: [Cell(v) for v in vals] for k, vals in rows.items()},
        source="rules",
        unit="crore",
    )


CONSISTENT = dict(
    revenue=[900, 810, 720],
    other_income=[100, 90, 80],
    total_income=[1000, 900, 800],
    total_expenses=[870, 780, 690],
    pbt_before_exceptional=[130, 120, 110],
    pbt=[130, 120, 110],
    tax=[30, 30, 30],
    pat=[100, 90, 80],
)


def test_hdfc_text_layer_rejected():
    ex = extract_rows(page("hdfcbank_std"))
    v = validate(ex)
    assert "total_income@0" in v.repaired
    assert ex.val("total_income", 0) == pytest.approx(97257.62, abs=0.01)
    assert "B4@0" in v.failed
    assert v.ok is False


def test_hdfc_truth_ok(hdfc_truth):
    v = validate(hdfc_truth)
    assert v.ok is True
    assert v.repaired == []
    assert v.failed == []


def test_rossari_ok_with_associates():
    assert validate(extract_rows(page("rossari_cons"))).ok is True


def test_indiacem_repairs_but_rejects():
    ex = extract_rows(page("indiacem_cons"))
    v = validate(ex)
    assert "other_income@0" in v.repaired
    assert ex.val("other_income", 0) == pytest.approx(3.67, abs=0.01)
    assert "finance_cost@2" in v.repaired
    assert ex.val("finance_cost", 2) == pytest.approx(25.27, abs=0.01)
    assert "C4@1" in v.failed
    assert v.ok is False


def test_bajajhfl_ok():
    assert validate(extract_rows(page("bajajhfl_std"))).ok is True


def test_kross_rejected():
    assert validate(extract_rows(page("kross_std"))).ok is False


def test_idbi_rejected():
    ex = extract_rows(page("idbi_std"))
    assert ex is None or validate(ex).ok is False


def test_consistent_synthetic_ok():
    assert validate(corporate(**CONSISTENT)).ok is True


def test_missing_pat_fails():
    rows = {k: v for k, v in CONSISTENT.items() if k != "pat"}
    assert validate(corporate(**rows)).ok is False


def test_eps_dropped_on_share_mismatch():
    ex = corporate(**CONSISTENT, eps=[10, 9, 80])
    v = validate(ex)
    assert [ex.val("eps", c) for c in range(3)] == [None, None, None]
    assert v.ok is True


def test_npa_out_of_range_cells_cleared(hdfc_truth):
    hdfc_truth.rows["nnpa_pct"][1] = Cell(2.5)  # nnpa > gnpa 1.17
    v = validate(hdfc_truth)
    assert hdfc_truth.val("gnpa_pct", 1) is None
    assert hdfc_truth.val("nnpa_pct", 1) is None
    assert hdfc_truth.val("gnpa_pct", 0) == 1.11
    assert v.ok is True


TRUTH0 = dict(
    revenue=900, other_income=100, total_income=1000, total_expenses=870,
    pbt_before_exceptional=130, exceptional=0, pbt=130, tax=30, pat=100,
)


def single(**overrides) -> Extraction:
    vals = {**TRUTH0, **overrides}
    return corporate(**{k: [v, None, None] for k, v in vals.items()})


def test_probe_e_pbt_and_pat_shifted_rejected():
    v = validate(single(pbt=140, pat=110))
    assert "C3@0" in v.failed
    assert v.ok is False


def test_probe_c_revenue_and_total_shifted_rejected():
    v = validate(single(revenue=1000, total_income=1100))
    assert "C2@0" in v.failed
    assert v.ok is False


def test_probe_j_total_expenses_typo_rejected():
    assert validate(single(total_expenses=800)).ok is False


def test_probe_a2_pat_never_repaired():
    ex = single(tax=35)
    ex.rows["pat"][0] = Cell(100, anomaly=True)
    v = validate(ex)
    assert v.ok is False
    assert ex.val("pat", 0) == 100
    assert "pat@0" not in v.repaired


def test_probe_h_column_present_via_any_term():
    ex = corporate(**{k: [v, v, None] for k, v in TRUTH0.items()})
    ex.rows["pat"][1] = Cell(None)
    ex.rows["total_income"][1] = Cell(None)
    ex.rows["pbt"][1] = Cell(999)
    v = validate(ex)
    assert "C3@1" in v.failed
    assert v.ok is False


def test_short_rows_do_not_crash():
    ex = single(total_expenses=800)
    ex.rows["total_income"] = [Cell(1000)]
    ex.rows["pbt_before_exceptional"] = [Cell(130), Cell(120)]
    assert validate(ex).ok is False
