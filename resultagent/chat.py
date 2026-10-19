"""Telegram chat analyst: remembers forwarded results (parsed exactly) and answers questions with an LLM
that may only use the saved numbers."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

from .config import Settings
from .http import Http, HttpError
import re

from .llm import LlmRouter
from .market import Market, describe
from .redbox import LABELS, Fact, fmt_change, fmt_value, parse
from .scoring import score
from .store import Store, now_ist
from .telegram import Telegram

log = logging.getLogger(__name__)

SYSTEM = """You are a senior Indian equity research analyst and swing/positional trader with 20 years of
experience in quarterly-results trading. You chat on Telegram with traders who forward result headlines.

Use ONLY the facts in the sections below — never invent or estimate a number. If data you need is
missing (e.g. no valuation data for a stock), say so in one line and work with what you have.

How you judge a result (in this order):
1. Growth quality: YoY revenue, EBITDA and profit growth together. Broad-based growth beats profit-only growth.
2. Margins: EBITDA-margin expansion (bps) = operating leverage = strong signal; contraction = warning.
3. Earnings quality: profit growing far faster than revenue, tiny bases, or turnarounds need caution.
4. Valuation: P/E (trailing), growth-adjusted P/E (P/E ÷ profit growth %; below 1 is cheap for the growth),
   P/B with ROE (high P/B needs high ROE). Expensive stocks need exceptional growth.
5. Price context: distance from 52-week high/low and today's move (already rallied hard? near high?).
6. Size/liquidity: market cap (small caps are riskier and more volatile).
The SCORE (0-100) is a fixed arithmetic results-quality score — use it as the anchor for ranking, then
adjust with valuation and price context and explain any change in order.

Answer format for picks/rankings (plain text, emojis ok, no markdown headings, no tables):
🏆 Pick: SYMBOL — one-line thesis
📈 Results: the key numbers with growth
💰 Valuation: P/E, growth-adjusted P/E, P/B/ROE in plain words
📊 Price: where it trades vs 52-week range, today's move
⚠️ Risks: 1-3 concrete risks
✅ Verdict: one line (e.g. strong result, reasonably priced / great result but expensive / avoid)
For a ranking, repeat a compact version for each stock (#1, #2, #3).
Default length: focused, ~120-250 words. In DEEP mode write a full research note (500-900 words) with the
same sections expanded, plus "What to watch next quarter".
Reply in the user's language. Always end with: "Not investment advice — verify before trading."
"""
DEEP_WORDS = ("deep", "detail", "detailed", "full analysis", "in depth", "elaborate", "explain fully")
HELP = ("I'm your results analyst.\n"
        "• Forward or paste result headlines (e.g. RedBox posts) — I save the exact numbers.\n"
        "• Ask anything: 'best result today?', 'top 3 this week', 'HFCL vs KROSS'. I add live price,\n"
        "  P/E, P/B, ROE and 52-week range from BSE. Say 'deep' for a full research note.\n"
        "Commands: /analyze SYMBOL (deep report) · /today · /list SYMBOL · /forget\n"
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
        self.market = Market(http, store) if http else None
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
        if text.lower().startswith("/analyze"):
            sym = text.partition(" ")[2].strip().upper()
            if not sym:
                await say("Usage: /analyze SYMBOL")
                return "command"
            await self.tg.typing(cid)
            await say(await self.answer(f"Give a deep, complete research analysis of {sym}.", cid, deep=True))
            return "answered"
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

    async def context(self, question: str) -> str:
        rows = self.store.facts(since=now_ist() - timedelta(days=45))
        groups = _group(rows)
        scored = sorted(((score(fs)[0], k, fs, saved) for k, (fs, saved) in groups.items()),
                        key=lambda x: (x[0], x[3]), reverse=True)[:150]
        # Live valuation for the stocks the question is about, plus the top-scored recent results.
        words = set(re.findall(r"[A-Z][A-Z0-9&.\-]{1,24}", question.upper()))
        saved_syms = [k[0] for _, k, _, _ in scored]
        wanted = [s for s in saved_syms if s in words]
        known = self.store.known_symbols() if self.market else set()
        wanted += [w for w in words if w in known and w not in wanted]
        wanted += [s for s in saved_syms if s not in wanted][: max(0, 8 - len(wanted))]
        snaps = await self.market.snapshots(wanted[:10]) if self.market and wanted else {}

        lines = []
        for sc, k, fs, saved in scored:
            notes = score(fs)[1]
            line = _line(k, fs, saved) + f" | SCORE {sc}/100" + (f" | notes: {'; '.join(notes)}" if notes else "")
            snap = snaps.get(k[0])
            pat = next((f for f in fs if f.metric == "pat" and f.basis == "yoy"), None)
            if snap and snap.get("pe") and pat and pat.change and pat.change > 0:
                line += f" | growth-adjusted P/E {snap['pe'] / pat.change:.2f}"
            lines.append(line)
        market = "\n".join(describe(s) for s in snaps.values()) or "(none fetched)"
        notes = "\n".join(f"- [{at[5:16]}] {t[:400]}" for t, at in self.store.notes()) or "(none)"
        return (f"Today is {now_ist():%Y-%m-%d %H:%M} IST.\n\n"
                f"RESULTS (saved, best score first):\n{chr(10).join(lines) or '(no results saved yet)'}\n\n"
                f"MARKET DATA (live from BSE; P/E is trailing 12 months):\n{market}\n\n"
                f"NOTES (forwarded news):\n{notes}")

    async def answer(self, question: str, cid: str = "", deep: bool = False) -> str:
        if not self.router:
            return "⚠️ No AI key configured, so I can only save numbers. Add GROQ_API_KEY to .env."
        deep = deep or any(w in question.lower() for w in DEEP_WORDS)
        system = SYSTEM + ("\nMODE: DEEP — write the full research note." if deep else "\nMODE: normal.")
        messages = [{"role": "system", "content": system + "\n\n" + await self.context(question)}]
        for role, t in self.store.chat(10, cid):
            messages.append({"role": role, "content": t})
        messages.append({"role": "user", "content": question})
        reply = await self.router.complete(messages, purpose="chat", timeout=90, json_mode=False,
                                           per_model=45, max_tokens=3500 if deep else 1500)
        if not reply:
            return "⚠️ All AI models are busy right now — please ask again in a minute."
        reply = re.sub(r"(?m)^#+\s*", "", reply.replace("**", "").replace("__", ""))
        reply = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", reply).strip()  # plain-text Telegram
        self.store.add_chat("user", question, cid)
        self.store.add_chat("assistant", reply, cid)
        return reply
