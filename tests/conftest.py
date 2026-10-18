import pytest

ALL_KEYS = {
    "TELEGRAM_BOT_TOKEN": "tok",
    "TELEGRAM_CHAT_ID": "123",
    "GROQ_API_KEY": "g",
    "OPENROUTER_API_KEY": "o",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(ALL_KEYS) + ["GEMINI_API_KEY"]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def make_env(tmp_path):
    def _make(**overrides):
        vals = {**ALL_KEYS, **overrides}
        p = tmp_path / ".env"
        p.write_text("\n".join(f"{k}={v}" for k, v in vals.items() if v is not None) + "\n")
        return p

    return _make


_HDFC_KEYS = (
    "interest_earned", "other_income", "total_income", "interest_expended", "opex", "ppop",
    "provisions", "exceptional", "pbt", "tax", "pat", "eps", "gnpa_pct", "nnpa_pct",
)
_HDFC_COLS = (
    (83426.60, 13831.02, 97257.62, 48807.42, 18966.78, 29483.42, 3573.62, 0, 25909.80,
     6399.84, 19509.96, 12.66, 1.11, 0.37),
    (79362.78, 12821.60, 92184.38, 45828.83, 18187.49, 28168.06, 3059.76, 0, 25108.30,
     6048.58, 19059.72, 12.38, 1.17, 0.41),
    (76690.70, 14350.02, 91040.72, 45139.20, 17977.92, 27923.60, 3500.53, 0, 24423.07,
     5781.79, 18641.28, 12.14, 1.24, 0.42),
)


@pytest.fixture
def hdfc_truth():
    from datetime import date

    from resultagent.models import Cell, Extraction, Template

    rows = {
        key: [Cell(float(col[i])) for col in _HDFC_COLS] for i, key in enumerate(_HDFC_KEYS)
    }
    return Extraction(
        template=Template.BANK,
        statement="standalone",
        period_end=date(2026, 9, 30),
        rows=rows,
        source="rules",
        unit="crore",
    )
