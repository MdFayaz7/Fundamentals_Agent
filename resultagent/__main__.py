"""CLI: python -m resultagent {run|test-telegram|get-chat-id} [--config config.toml] [--env .env]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from .chat import ChatBot
from .config import load_settings
from .http import Http
from .llm import LlmRouter
from .store import Store
from .telegram import Telegram


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)  # options work before or after the sub-command
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--env", default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="resultagent", parents=[common])
    p.set_defaults(config="config.toml", env=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "test-telegram", "get-chat-id"):
        sub.add_parser(name, parents=[common])
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = Path(a.env) if Path(a.env).exists() else None
    if a.cmd == "get-chat-id":
        asyncio.run(_chat_ids(env))
        return
    s = load_settings(Path(a.config), env)
    asyncio.run(_run(s) if a.cmd == "run" else _hello(s))


async def _run(s) -> None:
    http, store = Http(), Store(s.paths.db)
    tg = Telegram(http, s.telegram.bot_token, s.telegram.chat_id)
    router = LlmRouter(http, store, s.llm) if s.llm.models else None
    task = asyncio.create_task(ChatBot(s, store, http, router, tg).run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await http.close()
        store.close()


async def _hello(s) -> None:
    http = Http()
    mid = await Telegram(http, s.telegram.bot_token, s.telegram.chat_id).send_text("Hello from your results analyst ✅")
    print("sent, message_id =", mid)
    await http.close()


async def _chat_ids(env: Path | None) -> None:
    vals = {}
    if env:
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("\"'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Put TELEGRAM_BOT_TOKEN in .env first, then send any message to your bot.")
    http = Http()
    for cid, name in await Telegram(http, token, "").get_chat_ids():
        print(f"chat_id={cid}  ({name})")
    await http.close()


if __name__ == "__main__":
    main()
