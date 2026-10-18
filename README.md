# Results Flash Agent

24/7 agent for a Raspberry Pi that detects every NSE/BSE quarterly-results filing, reads the
sends an instant Telegram "results out" ping, then updates it with an exact report built from the
company's own XBRL filing (YoY/QoQ, margins, quality flags, score) — no PDF parsing, no AI needed.
Each result is also stored as a structured event for a future trading algo.

- Setup: [docs/SETUP.md](docs/SETUP.md)
- Design: [docs/superpowers/specs/2026-10-18-results-flash-agent-design.md](docs/superpowers/specs/2026-10-18-results-flash-agent-design.md)
- Tests: `.venv/bin/pytest -q`
