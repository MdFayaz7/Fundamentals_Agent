# Results Flash Agent

24/7 agent for a Raspberry Pi that detects every NSE/BSE quarterly-results filing, reads the
P&L from the PDF (rules first, free vision LLM for scanned PDFs), verifies the numbers
arithmetically, scores the quarter and sends a plain-English Telegram flash within ~1 minute.
Each result is also stored as a structured event for a future trading algo.

- Setup: [docs/SETUP.md](docs/SETUP.md)
- Design: [docs/superpowers/specs/2026-10-18-results-flash-agent-design.md](docs/superpowers/specs/2026-10-18-results-flash-agent-design.md)
- Tests: `.venv/bin/pytest -q`
