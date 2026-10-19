# Results Analyst Bot

A Telegram chat bot that runs 24/7 on a Raspberry Pi. Forward or paste quarterly-result headlines
(e.g. RedBox posts like `HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)`) — it saves the exact
numbers (no AI guessing). Then ask in plain words — "best result today?", "top 3 this week",
"HFCL vs KROSS" — and it answers like a fundamentals analyst, using only the numbers you saved.
It remembers everything (results, forwarded notes, recent conversation).

- Commands: `/today`, `/list SYMBOL`, `/forget`
- AI: free keys with automatic failover (Groq → Gemini → Mistral → OpenRouter)
- Setup: [docs/SETUP.md](docs/SETUP.md) · Tests: `.venv/bin/pytest -q`
