# Stock Analyst Bot (Telegram, Raspberry Pi)

A private Telegram analyst for Indian and US stocks, running 24/7 on a Raspberry Pi.

- **Results:** forward/paste result headlines (e.g. RedBox `HFCL: Q2 CONS NET PROFIT 3.6B RUPEES VS 678M (YOY)`)
  → exact numbers saved (no AI), scored 0–100 by fixed rules.
- **Any company:** ask about any NSE/BSE or US stock by symbol or name.
- **Evidence (computed in code, free data):** Yahoo Finance (trend of last ~5 quarters, P/E, forward P/E,
  EV/EBITDA, ROE, debt, ownership, analyst view, price returns, 50/200-DMA, RSI, volume spike) with BSE
  as fallback. The AI only interprets these numbers — it never invents them.
- **Answers:** 6 lines by default (pick, results & trend, valuation & quality, technicals, risk, verdict);
  `/analyze SYMBOL` for a full research note. Owner approves other users (`/allow`, `/deny`, `/users`).
- **AI:** free keys with automatic failover (Groq → Gemini → Mistral → OpenRouter).

Setup: [docs/SETUP.md](docs/SETUP.md) · Tests: `.venv/bin/pytest -q`
