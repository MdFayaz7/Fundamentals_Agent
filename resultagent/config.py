from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROVIDER_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
PROVIDER_ENV_KEYS = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


@dataclass(slots=True)
class TelegramCfg:
    bot_token: str
    chat_id: str


@dataclass(slots=True)
class PollCfg:
    fast_seconds: int = 10
    slow_seconds: int = 60
    fast_start: str = "08:00"
    fast_end: str = "23:59"


@dataclass(slots=True)
class FilterCfg:
    enabled: bool = False
    min_mcap_cr: float = 0
    watchlist: list[str] = field(default_factory=list)
    unknown_mcap_pass: bool = True


@dataclass(slots=True)
class ModelSpecCfg:
    name: str
    provider: str
    model: str
    vision: bool
    rpm: int
    rpd: int
    base_url: str = ""
    api_key: str = ""


@dataclass(slots=True)
class LlmCfg:
    models: list[ModelSpecCfg] = field(default_factory=list)
    commentary_reserve: float = 0.3


@dataclass(slots=True)
class PathsCfg:
    db: Path = Path("data/agent.db")


@dataclass(slots=True)
class Settings:
    telegram: TelegramCfg
    poll: PollCfg
    filter: FilterCfg
    llm: LlmCfg
    paths: PathsCfg
    scoring: dict[str, float]
    include_sme: bool
    backfill_on_start: bool
    webhook_url: str | None


def _read_env_file(path: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if path is None or not Path(path).exists():
        return out
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_settings(config_path: Path, env_path: Path | None) -> Settings:
    env = _read_env_file(env_path)
    env.update({k: v for k, v in os.environ.items() if k in _SECRET_KEYS})
    env = {k: v for k, v in env.items() if v != ""}

    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not env.get(key):
            raise ValueError(f"Missing required setting {key} (set it in .env)")

    raw = tomllib.loads(Path(config_path).read_text())

    models: list[ModelSpecCfg] = []
    llm_raw = raw.get("llm", {})
    for m in llm_raw.get("models", []):
        provider = m["provider"]
        key = env.get(PROVIDER_ENV_KEYS.get(provider, ""), "")
        if not key:
            continue
        models.append(
            ModelSpecCfg(
                name=m["name"],
                provider=provider,
                model=m["model"],
                vision=bool(m.get("vision", False)),
                rpm=int(m["rpm"]),
                rpd=int(m["rpd"]),
                base_url=PROVIDER_BASE_URLS[provider],
                api_key=key,
            )
        )

    paths_raw = raw.get("paths", {})
    return Settings(
        telegram=TelegramCfg(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]),
        poll=PollCfg(**raw.get("poll", {})),
        filter=FilterCfg(**raw.get("filter", {})),
        llm=LlmCfg(models=models, commentary_reserve=float(llm_raw.get("commentary_reserve", 0.3))),
        paths=PathsCfg(db=Path(paths_raw.get("db", "data/agent.db"))),
        scoring={k: float(v) for k, v in raw.get("scoring", {}).items()},
        include_sme=bool(raw.get("include_sme", False)),
        backfill_on_start=bool(raw.get("backfill_on_start", False)),
        webhook_url=raw.get("webhook_url") or None,
    )


_SECRET_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
}
