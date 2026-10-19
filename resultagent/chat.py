"""Telegram chat analyst: remembers forwarded results (parsed exactly) and answers questions with an LLM
that may only use the saved numbers."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

from .config import Settings
from .http import Http, HttpError
from .llm import LlmRouter
from .redbox import LABELS, Fact, fmt_change, fmt_value, parse
from .store import Store, now_ist
from .telegram import Telegram

log = logging.getLogger(__name__)

SYSTEM = """You are an expert Indian equity analyst who specialises in quarterly results (fundamentals).
You talk to one trader on Telegram. He forwards result headlines to you and asks which results are best.

Rules:
- Use ONLY the numbers in the DATA section. Never invent or estimate a figure. If something needed is
  missing, say exactly what is missing.
- When asked to pick or rank: give the ranked pick(s) first, then 2-4 short reasons each.
- Judge quality like a professional: YoY growth matters most; profit growth backed by revenue and
  EBITDA growth is better than profit growth alone; margin expansion is a strong positive; very small
  bases make huge % growth less meaningful; flag losses, falling margins or profit growing much faster
  than revenue (possible one-offs). For banks/NBFCs focus on NII, profit and NPA trend.
- "cons" = consolidated, "sl" = standalone. Amounts are ₹ crore. Margin changes are in bps.
- Be concise and Telegram-friendly: short lines, no tables, no markdown headings. Reply in the
  user's language.
- End every pick/ranking with: "Not investment advice — verify before trading."
"""
HELP = ("I'm your results analyst.\n"
        "• Forward or paste result headlines (e.g. RedBox posts) — I save the exact numbers.\n"
        "• Ask anything: 'best result today?', 'top 3 this week', 'HFCL vs KROSS'.\n"
        "Commands: /today · /list SYMBOL · /forget (clears chat history, keeps results)\n"
        "Owner only: /users · /allow ID · /deny ID")


def _group(rows: list[tuple[Fact, str]]) -> dict[tuple[str, str, str], tuple[list[Fact], str]]:
    out: dict = defaultdict(lambda: ([], ""))
    for f, saved in rows:
        key = (f.symbol, f.period, f.statement)
        facts, last = out[key]
        facts.append(f)
        out[key] = (facts, max(last, saved))
    return out


def _line(key, facts: list[Fact], saved: str = "") -> str:
    sym, period, st = key
    order = list(LABELS)
    facts = sorted(facts, key=lambda f: order.index(f.metric) if f.metric in order else 99)
    parts = [f"{LABELS.get(f.metric, f.metric)} {fmt_value(f)} ({fmt_change(f)} {f.basis.upper()})" for f in facts]
    head = f"{sym} {period}" + (f" ({st})" if st else "")
    return f"{head}: " + " · ".join(parts) + (f"  [saved {saved[5:16].replace('T', ' ')}]" if saved else "")


class ChatBot:
    def __init__(self, settings: Settings, store: Store, http: Http, router: LlmRouter | None, tg: Telegram):
        self.s, self.store, self.http, self.router, self.tg = settings, store, http, router, tg
        self.owner = str(tg.chat_id)
        self.env_allowed = set(settings.telegram.allowed_ids)

    async def run(self) -> None:
        offset = int(self.store.get("tg_offset", "0"))
        backoff = 1
        log.info("chat bot running")
        while True:
            try:
                updates = await self.tg.get_updates(offset)
                backoff = 1
            except HttpError as e:
                log.warning("getUpdates failed: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self.store.set("tg_offset", str(offset))
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                if not self.is_allowed(str(chat.get("id"))):
                    await self.request_access(chat)
                    continue
                try:
                    await self.handle(msg)
                except Exception:
                    log.exception("handling message failed")
                    await self.tg.send_text("⚠️ Something went wrong handling that message.", chat_id=str(chat.get("id")))

    async def handle(self, msg: dict) -> str:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        mid = msg.get("message_id")
        cid = str((msg.get("chat") or {}).get("id", self.tg.chat_id))
        say = lambda t: self.tg.send_text(t, mid, chat_id=cid)  # noqa: E731
        if not text:
            return "empty"
        if text.startswith("/"):
            await say(self.command(text, cid))
            return "command"
        facts = parse(text)
        if facts:
            self.store.save_facts(facts)
            groups = _group([(f, "") for f in facts])
            lines = [_line(k, fs) for k, (fs, _) in groups.items()]
            await say("✅ Saved\n" + "\n".join(lines))
            return "saved"
        if msg.get("forward_origin") or msg.get("forward_date"):
            self.store.add_note(text[:2000])
            await say("📝 Saved as a note (no result numbers found in it).")
            return "note"
        await self.tg.typing(cid)
        await say(await self.answer(text, cid))
        return "answered"

    # ---- access control: the owner (TELEGRAM_CHAT_ID) decides who may chat ----
    def is_allowed(self, cid: str) -> bool:
        u = self.store.user(cid)
        return cid == self.owner or (cid in self.env_allowed and not (u and u[1] == "denied")) \
            or bool(u and u[1] == "allowed")

    async def request_access(self, chat: dict) -> None:
        cid = str(chat.get("id"))
        name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or "?"
        if chat.get("username"):
            name += f" (@{chat['username']})"
        known = self.store.user(cid)
        if known:  # already asked (pending) or denied: stay silent
            return
        self.store.set_user(cid, "pending", name)
        log.info("access request from %s %s", cid, name)
        await self.tg.send_text(f"👤 {name} wants to use the bot.\nAllow: /allow {cid}\nDeny: /deny {cid}")
        await self.tg.send_text("🔒 This is a private bot. Your access request was sent to the owner.",
                                chat_id=cid)

    def admin(self, cmd: str, arg: str) -> str:
        if cmd == "/users":
            rows = self.store.users()
            return "\n".join(f"{s:8} {n} — {i}" for i, n, s in rows) or "No other users yet."
        uid = arg.strip()
        if not uid:
            return f"Usage: {cmd} USER_ID (see /users)"
        if uid == self.owner:
            return "That's you — the owner always has access."
        self.store.set_user(uid, "allowed" if cmd == "/allow" else "denied")
        return f"✅ {uid} can now use the bot." if cmd == "/allow" else f"⛔ {uid} is blocked."

    def command(self, text: str, cid: str = "") -> str:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/allow", "/deny", "/users"):
            return self.admin(cmd, arg) if cid == self.owner else "Only the owner can manage users."
        if cmd == "/today":
            rows = self.store.facts(since=now_ist().replace(hour=0, minute=0, second=0))
            groups = _group(rows)
            return ("📊 Saved today:\n" + "\n".join(_line(k, fs) for k, (fs, _) in groups.items())
                    if groups else "Nothing saved today yet. Forward some result posts.")
        if cmd == "/list":
            if not arg.strip():
                return "Usage: /list SYMBOL"
            groups = _group(self.store.facts(symbol=arg.strip()))
            return "\n".join(_line(k, fs, s) for k, (fs, s) in groups.items()) or f"No data saved for {arg.strip().upper()}."
        if cmd == "/forget":
            self.store.clear_chat(cid)
            return "🧹 Chat history cleared. Saved results are kept."
        return HELP

    def context(self) -> str:
        rows = self.store.facts(since=now_ist() - timedelta(days=45))
        groups = sorted(_group(rows).items(), key=lambda kv: kv[1][1], reverse=True)[:150]
        today = now_ist().date().isoformat()
        data = "\n".join(_line(k, fs, s) for k, (fs, s) in groups) or "(no results saved yet)"
        notes = "\n".join(f"- [{at[5:16]}] {t[:400]}" for t, at in self.store.notes()) or "(none)"
        return f"Today is {today}.\n\nDATA (saved results, newest first):\n{data}\n\nNOTES (forwarded news):\n{notes}"

    async def answer(self, question: str, cid: str = "") -> str:
        if not self.router:
            return "⚠️ No AI key configured, so I can only save numbers. Add GROQ_API_KEY to .env."
        messages = [{"role": "system", "content": SYSTEM + "\n\n" + self.context()}]
        for role, t in self.store.chat(10, cid):
            messages.append({"role": role, "content": t})
        messages.append({"role": "user", "content": question})
        reply = await self.router.complete(messages, purpose="chat", timeout=75, json_mode=False,
                                           per_model=35, max_tokens=1500)
        if not reply:
            return "⚠️ All AI models are busy right now — please ask again in a minute."
        reply = reply.replace("**", "").replace("__", "").strip()  # plain-text Telegram shows these literally
        self.store.add_chat("user", question, cid)
        self.store.add_chat("assistant", reply, cid)
        return reply
