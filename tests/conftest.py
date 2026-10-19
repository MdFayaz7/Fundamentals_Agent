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
