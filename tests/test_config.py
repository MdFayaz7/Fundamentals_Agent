from pathlib import Path

import pytest

from resultagent.config import load_settings

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_loads_example_config(make_env):
    s = load_settings(EXAMPLE, make_env())
    assert s.poll.fast_seconds == 10
    assert s.poll.slow_seconds == 60
    assert s.filter.enabled is False
    assert s.include_sme is False
    assert s.backfill_on_start is False
    assert s.llm.commentary_reserve == 0.3


def test_models_without_key_are_dropped(make_env):
    s = load_settings(EXAMPLE, make_env(OPENROUTER_API_KEY=None))
    assert s.llm.models
    assert not [m for m in s.llm.models if m.provider == "openrouter"]


def test_missing_telegram_token_raises(make_env):
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        load_settings(EXAMPLE, make_env(TELEGRAM_BOT_TOKEN=None))
