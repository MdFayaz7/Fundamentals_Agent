from datetime import date

from resultagent.periods import fallback_period_end, fy_label

def test_fallback_period():
    assert fallback_period_end(date(2026, 10, 17)) == date(2026, 9, 30)
    assert fallback_period_end(date(2026, 7, 1)) == date(2026, 6, 30)
    assert fallback_period_end(date(2026, 1, 5)) == date(2025, 12, 31)


def test_fy_label():
    assert fy_label(date(2026, 9, 30)) == "Q2 FY27"
    assert fy_label(date(2027, 3, 31)) == "Q4 FY27"
    assert fy_label(date(2026, 6, 30)) == "Q1 FY27"
    assert fy_label(date(2026, 12, 31)) == "Q3 FY27"


def test_fallback_on_quarter_end_is_previous_quarter():
    # a filing dated on a quarter end belongs to the previous quarter
    assert fallback_period_end(date(2026, 9, 30)) == date(2026, 6, 30)
