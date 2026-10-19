from pathlib import Path

import pytest

from resultagent.chat import ChatBot
from resultagent.config import load_settings
from resultagent.store import Store

HFCL = ("HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)\n"
        "HFCL: Q2 EBITDA 6.3B RUPEES VS 1.9B (YOY); Q2 EBITDA MARGIN 24.8% VS 18.4% (YOY)\n"
        "HFCL: Q2 REVENUE 25.4B RUPEES VS 10.4B (YOY)")


class FakeTg:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    async def send_text(self, text, reply_to=None, chat_id=None):
        self.sent.append(text)
        return len(self.sent)

    async def typing(self, chat_id=None):
        pass


class FakeRouter:
    def __init__(self, reply="HFCL is the best. Not investment advice — verify before trading."):
        self.reply, self.messages = reply, None

    async def complete(self, messages, **kw):
        self.messages = messages
        return self.reply


@pytest.fixture
def bot(make_env, tmp_path):
    s = load_settings(Path("config.example.toml"), make_env())
    return ChatBot(s, Store(tmp_path / "c.db"), None, FakeRouter(), FakeTg())


async def test_forwarded_results_saved_exactly(bot):
    assert await bot.handle({"message_id": 1, "text": HFCL}) == "saved"
    msg = bot.tg.sent[0]
    assert "HFCL Q2 (cons)" in msg and "Net profit ₹360 Cr (+431% YOY)" in msg
    assert "EBITDA margin 24.8% (+640 bps YOY)" in msg
    assert len(bot.store.facts(symbol="hfcl")) == 4


async def test_question_uses_saved_data_and_remembers_chat(bot):
    await bot.handle({"message_id": 1, "text": HFCL})
    assert await bot.handle({"message_id": 2, "text": "best result today?"}) == "answered"
    system = bot.router.messages[0]["content"]
    assert "HFCL Q2 (cons): Revenue ₹2,540 Cr (+144% YOY)" in system
    assert bot.tg.sent[-1].startswith("HFCL is the best")
    await bot.handle({"message_id": 3, "text": "why?"})
    roles = [m["role"] for m in bot.router.messages]
    assert roles == ["system", "user", "assistant", "user"]  # previous turn remembered


async def test_forwarded_news_becomes_note(bot):
    assert await bot.handle({"message_id": 1, "text": "Sebi tightens F&O rules", "forward_date": 1}) == "note"
    assert "Sebi tightens" in bot.context()


async def test_commands(bot):
    await bot.handle({"message_id": 1, "text": HFCL})
    assert bot.command("/today").startswith("📊 Saved today:")
    assert "HFCL Q2 (cons)" in bot.command("/list hfcl")
    assert "No data saved for XYZ" in bot.command("/list xyz")
    assert "Chat history cleared" in bot.command("/forget")


async def test_no_router_or_busy(bot):
    bot.router = FakeRouter(reply=None)
    await bot.handle({"message_id": 1, "text": "best?"})
    assert "busy" in bot.tg.sent[-1]
