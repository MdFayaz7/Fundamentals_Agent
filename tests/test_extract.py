from datetime import date
from pathlib import Path

import pytest

from resultagent.extract import ROW_KEYS, detect_statement, detect_template, extract_rows
from resultagent.models import Extraction, Template

FIX = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def check(ex: Extraction, expected: dict[str, list[float]]) -> None:
    for key, values in expected.items():
        got = [ex.val(key, i) for i in range(3)]
        assert got == pytest.approx(values, abs=0.01), key


def test_row_keys():
    assert ROW_KEYS[Template.CORPORATE] == (
        "revenue", "other_income", "total_income", "total_expenses", "finance_cost",
        "depreciation", "pbt_before_exceptional", "associates", "exceptional", "pbt",
        "tax", "pat", "pat_owners", "eps",
    )
    assert ROW_KEYS[Template.BANK] == (
        "interest_earned", "other_income", "total_income", "interest_expended", "opex",
        "ppop", "provisions", "exceptional", "pbt", "tax", "pat", "eps", "gnpa_pct",
        "nnpa_pct",
    )
    assert ROW_KEYS[Template.LENDER] == (
        "interest_income", "revenue", "other_income", "total_income", "finance_cost",
        "impairment", "total_expenses", "pbt_before_exceptional", "exceptional", "pbt",
        "tax", "pat", "eps",
    )


def test_hdfc_bank():
    ex = extract_rows(page("hdfcbank_std"))
    assert ex.template is Template.BANK
    assert ex.statement == "standalone"
    assert ex.period_end == date(2026, 9, 30)
    assert ex.source == "rules"
    assert ex.unit == "crore"
    assert all(len(cells) == 3 for cells in ex.rows.values())
    check(ex, {
        "interest_earned": [83426.60, 79362.78, 76690.70],
        "interest_expended": [48807.42, 45828.83, 45139.20],
        "other_income": [13831.02, 12821.60, 14350.02],
        "total_income": [97267.62, 92184.38, 91040.72],
        "opex": [18966.78, 18187.49, 17977.92],
        "ppop": [29483.42, 28168.06, 27923.60],
        "provisions": [3573.62, 3059.76, 3500.53],
        "pbt": [25909.80, 26108.30, 24423.07],
        "tax": [6399.84, 6048.58, 5781.79],
        "pat": [19609.96, 19069.72, 18641.28],
        "eps": [12.66, 12.38, 12.14],
        "gnpa_pct": [1.11, 1.17, 1.24],
        "nnpa_pct": [0.37, 0.41, 0.42],
    })


def test_rossari_corporate_million_to_crore():
    ex = extract_rows(page("rossari_cons"))
    assert ex.template is Template.CORPORATE
    assert ex.statement == "consolidated"
    assert ex.unit == "million"
    check(ex, {
        "revenue": [697.560, 697.200, 586.107],
        "other_income": [0.599, 3.187, 1.604],
        "total_income": [698.159, 700.387, 587.711],
        "total_expenses": [649.359, 653.172, 538.507],
        "finance_cost": [8.677, 10.977, 6.091],
        "depreciation": [30.096, 25.615, 18.233],
        "pbt_before_exceptional": [48.800, 47.215, 49.204],
        "associates": [0.469, 0.457, 0.468],
        "pbt": [49.269, 47.672, 49.672],
        "tax": [10.362, 12.577, 12.796],
        "pat": [38.907, 35.095, 36.876],
        "eps": [7.02, 6.34, 6.66],
    })


def test_indiacem_ocr_values():
    ex = extract_rows(page("indiacem_cons"))
    assert ex.template is Template.CORPORATE
    assert ex.statement == "consolidated"
    check(ex, {
        "revenue": [1142.03, 1019.42, 1117.04],
        "total_income": [1145.70, 1022.87, 1146.04],
        "total_expenses": [1093.42, 961.79, 1135.64],
        "exceptional": [0.0, 25.28, 6.01],
        "pbt": [52.28, 35.80, 4.39],
        "tax": [13.46, 8.35, -4.42],
        "pat": [38.82, 26.85, 8.81],
    })
    assert ex.val("other_income", 0) == pytest.approx(3.867, abs=0.01)
    assert ex.rows["other_income"][0].anomaly is True


def test_bajajhfl_lender():
    ex = extract_rows(page("bajajhfl_std"))
    assert ex.template is Template.LENDER
    assert ex.statement == "standalone"
    check(ex, {
        "interest_income": [3051.74, 2856.07, 2614.44],
        "revenue": [3250.57, 3063.02, 2748.04],
        "other_income": [0.52, 0.03, 0.15],
        "total_income": [3251.09, 3063.05, 2748.19],
        "finance_cost": [1996.65, 1888.49, 1657.96],
        "impairment": [34.78, 16.10, 42.85],
        "total_expenses": [2271.36, 2134.31, 1915.53],
        "pbt": [979.73, 928.74, 832.66],
        "tax": [222.76, 213.46, 189.70],
        "pat": [756.97, 715.28, 642.96],
    })


def test_idbi_column_layout_returns_garbage_not_crash():
    ex = extract_rows(page("idbi_std"))
    assert ex is None or isinstance(ex, Extraction)


def test_no_template():
    text = "Dear Sir/Madam, Outcome of the Board Meeting held today. Kindly take the same on record."
    assert detect_template(text) is None
    assert extract_rows(text) is None


def test_detect_statement():
    assert detect_statement("STATEMENT OF CONSOLIDATED RESULTS") == "consolidated"
    assert detect_statement("x" * 900 + " consolidated") == "standalone"


def corp(*lines: str) -> str:
    head = ["Statement of standalone results (Rs in crore)",
            "Revenue from operations 100.00 90.00 80.00"]
    return "\n".join(head + list(lines))


def test_pat_skips_discontinued_and_continuing_when_total_follows():
    ex = extract_rows(corp(
        "Profit for the period from continuing operations 15.00 14.00 13.00",
        "Profit for the period from discontinued operations 2.00 1.00 0.50",
        "Profit for the period 17.00 15.00 13.50",
    ))
    check(ex, {"pat": [17.00, 15.00, 13.50]})


def test_pat_continuing_kept_when_no_later_total():
    ex = extract_rows(corp(
        "Profit for the period from continuing operations 15.00 14.00 13.00",
        "Profit for the period from discontinued operations 2.00 1.00 0.50",
    ))
    check(ex, {"pat": [15.00, 14.00, 13.00]})


def test_pat_ignores_attributable_to_nci():
    ex = extract_rows(corp(
        "Net profit after tax attributable to non-controlling interests 1.00 1.00 1.00",
        "Profit after tax 10.00 9.00 8.00",
    ))
    check(ex, {"pat": [10.00, 9.00, 8.00]})


def test_pat_and_associates_with_slash_loss_labels():
    ex = extract_rows(corp(
        "Share of Profit/(Loss) of joint ventures 1.00 2.00 3.00",
        "Net Profit/(Loss) for the period 10.00 9.00 8.00",
    ))
    check(ex, {"pat": [10.00, 9.00, 8.00], "associates": [1.00, 2.00, 3.00]})
    ex = extract_rows(corp("Profit / (Loss) after tax 7.00 6.00 5.00"))
    check(ex, {"pat": [7.00, 6.00, 5.00]})


def test_current_tax_expense_is_not_total_tax():
    ex = extract_rows(corp(
        "Tax expense",
        "(a) Current tax expense 4.00 3.00 2.00",
        "(b) Deferred tax expense 1.00 1.00 1.00",
    ))
    check(ex, {"tax": [5.00, 4.00, 3.00]})


def test_tax_fallback_skips_discontinued_relating_and_net_of_tax():
    ex = extract_rows(corp(
        "Current tax 3.00 2.00 1.00",
        "Deferred tax 1.00 1.00 1.00",
        "Income tax relating to items not reclassified tax expense 0.50 0.40 0.30",
        "Tax expense of discontinued operations - - -",
        "Extraordinary items (net of tax expense) - - -",
    ))
    check(ex, {"tax": [4.00, 3.00, 2.00]})


def test_before_guard_for_associates_and_exceptional():
    ex = extract_rows(corp(
        "Profit before share of profit of associates and exceptional items 50.00 40.00 30.00",
        "Share of profit of associates 1.00 2.00 3.00",
        "Profit before exceptional items and tax 51.00 42.00 33.00",
        "Exceptional items 5.00 - 2.00",
    ))
    check(ex, {
        "pbt_before_exceptional": [50.00, 40.00, 30.00],
        "associates": [1.00, 2.00, 3.00],
        "exceptional": [5.00, 0.0, 2.00],
    })


def test_bracketed_integer_negatives_survive():
    ex = extract_rows(corp("Exceptional items (125) - (60) (125) (60)"))
    check(ex, {"exceptional": [-125, 0, -60]})
