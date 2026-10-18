# Results Flash Agent — Design

Date: 2026-10-18 · Status: approved (user delegated remaining choices to Claude)

## 1. Intent

The user is a professional Indian equity trader who trades on fundamentals. When a
listed company (NSE/BSE) files quarterly/annual results, they want a **short,
easy-to-read analysis on Telegram within ~1 minute of the filing**, 24/7, from a
Raspberry Pi 5 (2 GB RAM, static IP Wi-Fi) at home.

Later (separate project) a "very good" result will feed an **algo buy trigger**.
So every alert must also exist as a clean, machine-readable event with an
explicit confidence level — the algo must never act on numbers we are not sure of.

## 2. Goals / non-goals / anti-goals

**Goals (v1)**
- Detect every new financial-results filing on NSE and BSE (all ~5,000 companies).
- Extract the P&L numbers from the result PDF, verify them arithmetically, compute
  YoY/QoQ, quality flags, a verdict and a 1–10 score.
- Send one Telegram "flash" message per company per result, fast (target: alert
  ≤ 60 s after the exchange disseminates the PDF, for text PDFs).
- Optional AI commentary from free LLM APIs (Groq, OpenRouter) that never delays
  or blocks the flash.
- Persist each analysed result as a structured event (for the future algo).
- Company filter hook (watchlist / min market-cap) — built but **off** by default.
- Run unattended for months on the Pi: auto-restart, low memory, low SD-card wear.

**Non-goals (v1)**
- No second "verified from XBRL" alert (user decided flash is enough).
- No buy/sell execution, no broker integration (future algo project).
- No local OCR engine (tesseract). Scanned/image-only result pages are read by a
  free **vision LLM** instead (see §5 item 8); if no vision capacity, headline-only.
- No analyst-consensus "beat/miss" (needs paid data); judge vs company's own history.
- No WhatsApp (Telegram only; WhatsApp bots need paid Business API).
- No web dashboard.

**Anti-goals (failure even if it "works")**
- Sending **wrong numbers** with full confidence. Garbled extraction must be
  caught by the arithmetic checks and downgraded, never sent as "verified".
- Duplicate alerts (same result on NSE + BSE, re-posted announcements, restarts).
- Flash delayed because an LLM is slow or rate-limited.
- Getting the Pi's IP blocked by NSE/BSE through aggressive polling.
- Spamming non-result announcements (dividend notices, board meeting intimations).

## 3. Research findings (verified live on 2026-10-17/18 from an Indian IP)

| Source | Endpoint | Notes |
|---|---|---|
| NSE announcements | `GET https://www.nseindia.com/api/corporate-announcements?index=equities[&from_date=dd-mm-yyyy&to_date=..][&symbol=X]` | JSON list. Fields: `symbol, sm_name, sm_isin, desc, attchmntText, attchmntFile (PDF), an_dt, exchdisstime, seq_id`. Results appear as `desc="Outcome of Board Meeting"` (also "Financial Result Updates") with `attchmntText` containing "financial result". Header `Referer: https://www.nseindia.com/`. Works with curl_cffi Chrome impersonation; homepage warm-up may 403 but API still 200. |
| BSE announcements | `GET https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=Result&strPrevDate=YYYYMMDD&strToDate=YYYYMMDD&strScrip=&strSearch=P&strType=C&subcategory=-1` | **Requires Chrome TLS impersonation (curl_cffi)**; plain curl/httpx → Akamai 403. Headers `Referer/Origin: https://www.bseindia.com`. `Table[]` fields: `NEWSID, SCRIP_CD, SLONGNAME, NEWSSUB, NEWS_DT, DissemDT, ATTACHMENTNAME, SUBCATNAME ("Financial Results")`. PDF: `https://www.bseindia.com/xml-data/corpfiling/AttachLive/<ATTACHMENTNAME>`. |
| BSE scrip master | `GET https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active` | 5,112 rows, 1.8 MB: `SCRIP_CD, ISIN_NUMBER, scrip_id, Scrip_Name, Mktcap (₹ Cr), GROUP`. Used for BSE→ISIN mapping (dedupe) and the market-cap filter. Refresh daily. |
| NSE integrated-filing XBRL | `/api/integrated-filing-results?...` | Exact numbers but arrives **25 min – 4 h after the PDF** (HDFC Bank: PDF 13:56, XBRL 17:46). Too slow → not used in v1. Kept as a documented option. |

PDF facts (6 samples: HDFCBANK, INDIACEM, KROSS, ROSSARI, LAGNAM, IDBI):
- 1–7 MB typical (one BSE PDF was 27 MB), 6–24 pages, download 1–5 s, text
  extraction with `pypdfium2` 0.02–0.2 s. All had a text layer.
- The results table has the standard SEBI layout: columns **Quarter ended (current),
  Quarter ended (previous), Quarter ended (same quarter last year), Half/9M (current),
  Half/9M (previous), Year ended**. So QoQ and YoY come from the same PDF — no
  history database needed.
- Clean PDFs (HDFC) extract perfectly row-by-row. OCR'd scans (KROSS) produce
  garbled numbers ("235092" for "2,350.92") — must be detected and rejected.
- Units vary per PDF: "₹ in crore", "Rs. in lakhs", "Rs. Millions" — must be detected
  and normalised to ₹ crore.
- Most PDFs contain both standalone and consolidated tables.
- Bank PDFs use a different statement (Interest earned, Interest expended,
  Operating profit, Provisions, GNPA/NNPA %).

**Scanned PDFs are common (measured on 21 random BSE result PDFs, 10–17 Oct 2026):**
~50% have a typed cover letter but the results table is an **image** (e.g. HCL Tech,
Tejas Networks, HDB Financial). Text extraction finds nothing on those. Some others
have a poor OCR text layer (IDBI, Kross). So a vision path is required for useful
coverage. Free OpenRouter vision models seen 2026-10-18: `google/gemma-4-31b-it:free`,
`google/gemma-4-26b-a4b-it:free`, `nvidia/nemotron-3.5-super-vl:free`.

Free LLM limits (Oct 2026): Groq (OpenAI-compatible, `https://api.groq.com/openai/v1`)
~30 RPM / ~1K RPD / 100–200K tokens per day **per model**; JSON mode supported.
OpenRouter (`https://openrouter.ai/api/v1`) `:free` models 20 RPM, **50 RPD** (1,000 RPD
after a one-time $10 credit purchase); the free-model list changes often. Peak
season is 300–500 results/day, so LLM capacity is scarce and must be budgeted.

## 4. Decisions made with the user

- Delivery: **Telegram** bot.
- Coverage: **all** companies now; filter (watchlist / market cap) to be enabled later.
- Report content/metrics: delegated to Claude ("you know better") — see §6.
- LLMs: **free API keys only — Groq and OpenRouter** (user will supply keys).
- Speed over exactness: **flash alert from the PDF only**; no XBRL follow-up.
- Future: good results will trigger an algo buy → events must be structured.
- Hardware: Raspberry Pi 5, 2 GB RAM, static-IP Wi-Fi.

## 5. Architecture (Claude's call)

Single Python 3.11+ asyncio process, one systemd service. Minimal dependencies:
`curl_cffi` (HTTP with Chrome TLS fingerprint — NSE, BSE, Telegram, LLMs),
`pypdfium2` (PDF text + page rendering) and `Pillow` (JPEG encode of rendered pages). Stdlib `sqlite3` for state. Both deps ship aarch64 wheels.

```
 NSE poller ─┐                                       ┌─> Telegram sendMessage (flash)
             ├─> dedupe/filter ─> PDF fetch ─> extract ─> validate ─> analyse ─┤
 BSE poller ─┘   (SQLite seen)    (bounded)   rules→LLM   arithmetic   score    ├─> events table (algo)
                                                                               └─> LLM commentary ─> editMessageText
```

Components (one module each):
1. **Exchange clients** — NSE and BSE pollers. Each poll fetches only today's newest
   page. Adaptive interval: 10 s from 08:00–23:59 IST, 60 s overnight; jitter ±20%.
   Exponential backoff on 403/429/5xx, session re-warm on 401/403. Never more than
   one in-flight request per exchange for listing calls.
2. **Result detector** — keeps only result filings: BSE `SUBCATNAME=="Financial Results"`;
   NSE `desc` ∈ {Outcome of Board Meeting, Financial Result Updates, Financial Results}
   AND `attchmntText` matches `/financial result/i`. Final guard: if no P&L table
   is found in the PDF, drop silently (logged).
3. **Dedupe** — key = ISIN (BSE via scrip master) + quarter-end date. First exchange
   to disseminate wins; the other is recorded and ignored. Re-posts/revisions of the
   same quarter are ignored in v1 (logged). Survives restarts (SQLite).
4. **Filter hook** — `config: filter.enabled=false, min_mcap_cr, watchlist[]`.
   Applied after dedupe, before PDF download (saves bandwidth).
5. **PDF fetch** — stream to memory with 40 MB cap, 30 s timeout, max 3 concurrent.
   Not written to disk (SD-card wear). Text via pypdfium2, page by page.
6. **Extractor (rules first)** — locate results page(s) by keywords; classify each as
   standalone/consolidated and bank/non-bank; detect units; regex row labels
   (with synonym lists) and take the first three numeric columns (current Q,
   previous Q, same Q last year). Parses Indian number formats, brackets as
   negatives, "-" as 0. Also reads the period header to confirm column order.
7. **Validator** — arithmetic identities with ±1% tolerance, e.g. non-bank:
   `Revenue + Other income ≈ Total income`, `Total income − Total expenses ≈ PBT
   before exceptional`, `PBT − Tax ≈ PAT (± associates/discontinued)`; bank:
   `Interest earned + Other income ≈ Total income`, `Total income − Interest
   expended − Opex ≈ Operating profit`. Each column validated independently.
8. **Vision fallback extractor** — only if rules fail validation (or find nothing).
   Many "text" PDFs carry an invisible OCR text layer with digit slips (verified:
   HDFC Bank's text says PAT 19,609.96 Cr, XBRL truth 19,509.96; India Cements tax
   8.35 vs 8.95). Re-reading bad text with an LLM cannot fix that, so the fallback
   always **looks at the page image**: render the rule-selected result page (if one
   was found) or up to 2 candidate image-only pages among the first 8, at ~150 dpi,
   grayscale JPEG (~150–300 KB each), one request to a vision model, strict JSON in
   the same schema, then the same arithmetic checks. Same 25 s budget (§10a).
9. **Analyser (deterministic)** — metrics, flags, verdict, score (§6).
10. **Notifier** — Telegram HTML message (§7). Retries with backoff; respects
    Telegram's 429 `retry_after`.
11. **LLM commentary (async, optional)** — after the flash is sent, request 3–4
    bullet insights from the computed numbers only (~600 tokens). On success,
    `editMessageText` appends "🤖 AI view". 20 s budget; skipped silently if no
    capacity.
12. **LLM router** — ordered provider/model list from config (default Groq
    `llama-3.3-70b-versatile`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
    `llama-3.1-8b-instant`, then OpenRouter free models). Tracks per-model daily
    and per-minute usage, honours `retry-after`, skips exhausted models.
    Each model entry declares `vision: bool`. **Priority: vision extraction >
    commentary** (text-only models are used for commentary). Commentary only when remaining
    daily budget is above a reserve (e.g. 30%).
13. **Store** — SQLite (WAL mode) with tables `seen_filings`, `results`, `events`,
    `llm_usage`, `scrips`. Event rows are JSON — the future algo reads them (or a
    configurable webhook URL receives a POST; off by default).
14. **Health** — daily heartbeat to Telegram (08:30 IST: "alive, N results
    yesterday, errors"), and an alert if a poller fails continuously for 10 min.
    Logs to journald, size-capped.

Resource targets: RSS < 200 MB steady, CPU idle < 5%, no PDF on disk.

## 6. Analysis content (Claude's call, as the "trader")

Primary statement: **consolidated** for non-banks if present, else standalone;
**standalone** for banks (Indian convention; NPA data is standalone).

Non-bank metrics (₹ crore): Revenue from operations, EBITDA (= PBT + finance cost +
depreciation − other income, before exceptional), EBITDA margin, PAT (attributable
to owners when available), EPS. Each with YoY % and QoQ %, margin change in bps.

Bank metrics: NII (Interest earned − Interest expended), Other income, Operating
profit (PPOP), Provisions, PAT, GNPA %, NNPA % (when present), each with YoY/QoQ.

Quality flags (✅ positive / ⚠️ caution):
- PAT growth ≫ revenue growth driven by other income (other income > 25% of PBT).
- Exceptional items > 10% of PBT (one-off).
- Effective tax rate < 15% or > 35%, or swing > 10 pts vs year ago.
- EBITDA margin expansion/contraction > 100 bps YoY.
- Revenue and PAT both growing > 15% YoY (strong), or both declining.
- Loss to profit / profit to loss turnarounds.
- Banks: provisions growth vs PPOP growth; GNPA/NNPA direction.

**Plain language (user requirement):** the user is not a fundamentals expert. Every flag
is written as a plain-English sentence (e.g. "Profit rose mainly because of one-time
income, not the core business"), and each alert ends with one **"What this means:"**
line summarising the result in simple words (rule-generated; the AI view may replace
it with a better sentence).

Score 1–10 and verdict (STRONG / GOOD / MIXED / WEAK / POOR) from a transparent
weighted rule set (YoY revenue, YoY PAT, margin trend, quality flags, QoQ). Weights
live in config so the user can tune them.

Confidence on every result: `VERIFIED` (all identities pass on rule extraction),
`LLM_VERIFIED` (LLM extraction passed identities), `UNVERIFIED` (failed — numbers
not shown; headline + PDF link only), `NO_TEXT` (scanned PDF — headline only).
The future algo must only consider `VERIFIED`/`LLM_VERIFIED`.

## 7. Telegram message format (example uses real HDFC Bank Q2 numbers; NPA values illustrative)

```
🟢 HDFCBANK · HDFC Bank Ltd · Q2 FY27 (Standalone) · Bank
Verdict: GOOD · Score 7/10 · ✔ verified

NII         ₹34,619 Cr  YoY +9.7%   QoQ +3.2%
PPOP        ₹29,483 Cr  YoY +5.6%   QoQ +4.7%
Provisions  ₹3,574 Cr   YoY +2.1%   QoQ +16.8%
PAT         ₹19,610 Cr  YoY +5.2%   QoQ +2.8%
GNPA 1.24% (↓) · NNPA 0.42%

✅ Core income growing faster than provisions
⚠️ Provisions up 17% QoQ

📄 PDF · NSE 13:56:22 · alert 13:56:51 (29 s)
```
Colour emoji by verdict (🟢 strong/good, 🟡 mixed, 🔴 weak/poor, ⚪ unverified).
One message per company-result, ≤ ~1,000 chars, monospace block for numbers.

## 8. Configuration & secrets

`config.toml` (non-secret, documented defaults) + `.env` (secrets:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`),
`.env` chmod 600 and git-ignored. LLM keys optional — without any, the agent still
runs rules-only. **Recommended:** add a one-time $10 OpenRouter credit (raises the
free-model cap from 50 to 1,000 requests/day — needed for the vision path in peak
season) and optionally a free Google Gemini key (OpenAI-compatible endpoint
`https://generativelanguage.googleapis.com/v1beta/openai/`) as extra vision capacity.

## 9. Testing

- Unit tests on real fixture text from the sampled PDFs (HDFCBANK, INDIACEM,
  ROSSARI, KROSS-garbled → must be rejected) plus synthetic tables.
- Number parsing, unit detection, validator, analyser, formatter, dedupe — pure
  functions, fully unit-tested.
- Exchange/Telegram/LLM clients tested against recorded JSON fixtures (no network in
  tests). One opt-in live smoke test script.
- A `--replay DATE` mode re-processes a past day's filings without sending (or
  sending to a test chat) to measure extraction hit-rate.

## 10. Deployment (Pi)

Raspberry Pi OS Lite 64-bit (Bookworm/Trixie), Python venv, systemd unit with
`Restart=always`, `MemoryMax=600M`, run as unprivileged user. SQLite on SD card in
WAL mode (tiny writes). Setup guide covers: creating the Telegram bot (BotFather),
getting chat id, Groq/OpenRouter keys, install, update, logs. Inbound ports: none
(static IP not required — all connections are outbound).

## 10a. Clarifications from builder review (Claude's calls unless marked)

1. **LLM fallback on the critical path:** the flash waits for the LLM extractor at
   most **25 s** (hard timeout). On timeout/failure the flash goes out as
   `UNVERIFIED` (headline + PDF link). No later upgrade in v1 — each result emits
   exactly **one** final event, so the algo contract stays simple (events are
   immutable; schema has `event_version: 1`).
2. **Dedupe in two stages:** pre-download key = ISIN + IST filing date + state
   (`processing`/`done`). If an ISIN already has a `done` alert today, the other
   exchange's filing is skipped without download. If the PDF had no results
   table (cover letter, intimation), the state is not `done`, so a later real
   filing on the same day is still processed. Final key = ISIN + quarter-end date.
   Concurrent duplicates: an in-memory per-ISIN lock; the second waits and
   re-checks.
3. **Scope of companies:** main-board equity on NSE and BSE. SME and debt-only
   issuers are excluded by default (BSE scrip-master groups `M`, `MT`, `MS` etc.
   and NSE `index=equities` only), behind a config switch `include_sme=false`.
   *(User delegated this choice to Claude on 2026-10-18; SME excluded in v1.)*
4. **Templates:** three templates.
   - `bank` — banks (Bank P&L layout detected).
   - `lender` — NBFCs/HFCs (detected by "Interest income"/"Revenue from
     operations: Interest income" + "Impairment on financial instruments"): Total
     revenue, Finance cost, NII = interest income − finance cost, Impairment, PAT;
     no EBITDA.
   - `corporate` — everyone else (EBITDA etc.).
   Insurers and anything that matches no template → `corporate` attempt; if
   validation fails → headline-only with PAT/Total income if those two rows parse.
5. **Split standalone/consolidated filings:** alert on the first result filing. If
   a later filing for the same ISIN + quarter contains a consolidated table the
   first lacked (and the company is non-bank), send one follow-up flash marked
   "Consolidated" and emit a second event with `statement=consolidated`. Otherwise
   ignore re-filings.
6. **Test fixtures:** the sample PDFs are in the session scratchpad and can be
   re-downloaded from NSE (any Indian IP, curl_cffi). Extracted page text (not the
   PDFs) is committed under `tests/fixtures/` — small and deterministic.
7. **Telegram target:** a private chat with the user (default). A send queue
   throttles to ≤ 1 msg/s and ≤ 18 msg/min so it is also safe for a group/channel.

## 11. Left to the builder

Exact regexes/synonym lists, the score weights' initial values, module layout,
retry constants, SQLite schema details, message wording, test fixture selection.

## 12. Future (not v1)

Algo trigger consumer of `events`; Telegram commands (/filter, /watch, /mute);
XBRL-based verification / fallback for unreadable PDFs; local OCR; WhatsApp; con-call/investor
presentation summaries.
