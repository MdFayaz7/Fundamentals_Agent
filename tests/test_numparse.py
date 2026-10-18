from datetime import date
from pathlib import Path

import pytest

from resultagent.numparse import (
    UNIT_DIVISORS,
    detect_period_end,
    detect_unit_divisor,
    fallback_period_end,
    fy_label,
    tokenize_numbers,
)

FIX = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


def vals(s):
    return [c.value for c in tokenize_numbers(s)]


def test_plain_row():
    cells = tokenize_numbers("1,142.03 1,019.42 1,117.04 2,161.45")
    assert [c.value for c in cells] == pytest.approx([1142.03, 1019.42, 1117.04, 2161.45])
    assert not any(c.anomaly for c in cells)


def test_brackets_braces_dash():
    assert vals("{25.07) (12.51) 43.61 - (122,54)") == pytest.approx(
        [-25.07, -12.51, 43.61, 0.0, -122.54]
    )


def test_decimal_comma():
    assert vals("79362,78 45139,20") == pytest.approx([79362.78, 45139.20])


def test_dotted_thousands():
    a = tokenize_numbers("3.051.74 2,856.07")
    assert a[0].value == pytest.approx(3051.74) and a[0].anomaly
    assert a[1].value == pytest.approx(2856.07) and not a[1].anomaly
    b = tokenize_numbers("222..76 213.46")
    assert b[0].value == pytest.approx(222.76) and b[0].anomaly
    assert b[1].value == pytest.approx(213.46)


def test_three_decimals_flagged():
    c = tokenize_numbers("3.867 3.45 29.00")
    assert c[0].value == pytest.approx(3.867) and c[0].anomaly
    assert not c[1].anomaly and not c[2].anomaly


def test_decimal_loss_flagged():
    c = tokenize_numbers("24.08 26.15 2527 50.21")
    assert c[2].value == 2527 and c[2].anomaly
    assert not c[0].anomaly and not c[3].anomaly


def test_formula_refs_removed():
    assert vals("(3-4} 52,28 61.08 10.40") == pytest.approx([52.28, 61.08, 10.40])
    assert vals("(4) 1,093.42 961,79 1,135.64") == pytest.approx([1093.42, 961.79, 1135.64])
    assert vals("(Refer note 12) 13831.02 12821.60") == pytest.approx([13831.02, 12821.60])


def test_letters_none():
    c = tokenize_numbers("8B.08 0.91")
    assert c[0].value is None and c[0].anomaly
    assert c[1].value == pytest.approx(0.91)


def test_stops_at_next_label():
    assert vals("- - VII I Profit before tax [V-VI] 492.69 476.72") == [0.0, 0.0]


def test_letter_formula_ignored():
    assert vals("(a)+(b)+(c)+(d) 83426.60 79362,78") == pytest.approx([83426.60, 79362.78])


def test_label_tail_skipped():
    assert vals("ltems-Expense/(income) - 25.28 6.01") == pytest.approx([0.0, 25.28, 6.01])


def test_percent_stripped():
    assert vals("1.11% 2.50%") == pytest.approx([1.11, 2.50])


def test_units():
    assert detect_unit_divisor(page("hdfcbank_std")) == ("crore", 1.0)
    assert detect_unit_divisor(page("indiacem_cons")) == ("crore", 1.0)
    assert detect_unit_divisor(page("rossari_cons")) == ("million", 10.0)
    assert detect_unit_divisor(page("kross_std")) == ("million", 10.0)
    assert detect_unit_divisor(page("bajajhfl_std")) == ("crore", 1.0)
    assert detect_unit_divisor("(Rs. in lakhs)") == ("lakh", 100.0)
    assert detect_unit_divisor("nothing here") == ("crore", 1.0)
    assert UNIT_DIVISORS["rupees"] == 1e7


def test_period():
    assert detect_period_end(page("hdfcbank_std")) == date(2026, 9, 30)
    assert detect_period_end(page("rossari_cons")) == date(2026, 9, 30)
    assert detect_period_end(page("bajajhfl_std")) == date(2026, 9, 30)
    assert detect_period_end("quarter ended 31.12.2026") == date(2026, 12, 31)
    assert detect_period_end("quarter ended 30-Jun-2026") == date(2026, 6, 30)
    assert detect_period_end("no dates") is None


def test_fallback_period():
    assert fallback_period_end(date(2026, 10, 17)) == date(2026, 9, 30)
    assert fallback_period_end(date(2026, 7, 1)) == date(2026, 6, 30)
    assert fallback_period_end(date(2026, 1, 5)) == date(2025, 12, 31)


def test_fy_label():
    assert fy_label(date(2026, 9, 30)) == "Q2 FY27"
    assert fy_label(date(2027, 3, 31)) == "Q4 FY27"
    assert fy_label(date(2026, 6, 30)) == "Q1 FY27"
    assert fy_label(date(2026, 12, 31)) == "Q3 FY27"


def test_unit_ignores_lac_inside_words():
    t = "Registered Office: Palace Road, Black Town\n(Rs. in crore)"
    assert detect_unit_divisor(t) == ("crore", 1.0)


def test_unit_header_beats_prose():
    t = "Note: penalty of Rs. 5 lakhs was paid.\n(₹ in million)"
    assert detect_unit_divisor(t) == ("million", 10.0)


def test_unit_lacs_and_rupees():
    assert detect_unit_divisor("(Rs. in Lacs)")[0] == "lakh"
    assert detect_unit_divisor("(Amount in rupees)") == ("rupees", 1e7)
    assert detect_unit_divisor("Place of black palace") == ("crore", 1.0)


def test_fallback_on_quarter_end_is_previous_quarter():
    # a filing dated on a quarter end belongs to the previous quarter
    assert fallback_period_end(date(2026, 9, 30)) == date(2026, 6, 30)
