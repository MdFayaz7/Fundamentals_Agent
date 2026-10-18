"""CLI: python -m resultagent {run|replay|test-telegram|get-chat-id}"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date
from pathlib import Path

from .app import App, replay
from .config import load_settings
from .http import Http
from .telegram import Telegram


def main() -> None:
    p = argparse.ArgumentParser(prog="resultagent")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--env", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    r = sub.add_parser("replay")
    r.add_argument("--date", required=True, type=date.fromisoformat)
    r.add_argument("--send", action="store_true")
    sub.add_parser("test-telegram")
    sub.add_parser("get-chat-id")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)

    env = Path(a.env) if Path(a.env).exists() else None
    if a.cmd == "get-chat-id":
        asyncio.run(_chat_ids(env))
        return
    s = load_settings(Path(a.config), env)
    if a.cmd == "run":
        asyncio.run(App(s).run())
    elif a.cmd == "replay":
        asyncio.run(replay(s, a.date, a.send))
    elif a.cmd == "test-telegram":
        asyncio.run(_hello(s))


async def _hello(s) -> None:
    http = Http()
    mid = await Telegram(http, s.telegram.bot_token, s.telegram.chat_id).send("Hello from Results Agent ✅")
    print("sent, message_id =", mid)
    await http.close()


async def _chat_ids(env: Path | None) -> None:
    import os
    vals = {}
    if env:
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in .env first, then send any message to your bot.")
    http = Http()
    for cid, name in await Telegram(http, token, "").get_chat_ids():
        print(f"chat_id={cid}  ({name})")
    await http.close()


if __name__ == "__main__":
    main()
