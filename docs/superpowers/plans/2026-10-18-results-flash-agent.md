# Results Flash Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 24/7 Raspberry Pi service that detects every NSE/BSE financial-results filing, extracts and verifies the P&L numbers from the PDF (rules → vision LLM on the page image), analyses them, and sends a plain-English Telegram flash within ~1 minute, while storing a structured event for a future algo.

**Architecture:** One asyncio process. Pollers produce `Filing`s → pipeline (dedupe, filter, PDF fetch, extract, validate/repair, LLM fallback, analyse) → Telegram queue + SQLite events. Pure-function core (numparse/extract/validate/analyse/fmt) is fully unit-tested on real PDF text fixtures; I/O edges are thin and tested with fakes.

**Tech Stack:** Python ≥3.11 (stdlib `asyncio`, `sqlite3`, `tomllib`), `curl_cffi` (Chrome TLS impersonation, async), `pypdfium2` (text + render), `Pillow` (JPEG). Dev: `pytest`, `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-10-18-results-flash-agent-design.md` (read it first; §10a has binding clarifications).

## Global Constraints

- Runtime deps are exactly: `curl_cffi>=0.7`, `pypdfium2>=4.30`, `Pillow>=10`. Nothing else at runtime.
- All network calls go through `resultagent/http.py` (curl_cffi `AsyncSession(impersonate="chrome")`).
- All amounts inside the program are **₹ crore** floats after normalisation (EPS in ₹, NPA in %).
- Column index convention everywhere: `0` = current quarter, `1` = previous quarter (QoQ base), `2` = same quarter last year (YoY base). Missing → `None`.
- PDFs are never written to disk; max 40 MB, 30 s timeout, ≤3 concurrent downloads.
- Flash path budget: LLM extraction hard timeout **25 s**; commentary **20 s**, after the flash, via `editMessageText`.
- Telegram send queue: ≤1 msg/s and ≤18 msg/min; honour `retry_after` on 429.
- Polling: 10 s (08:00–23:59 IST), 60 s otherwise, ±20 % jitter; exponential backoff on errors (cap 5 min).
- Timezone: all user-facing times IST (`Asia/Kolkata` via `zoneinfo`).
- SME excluded by default: BSE scrip groups `M`, `MT`, `MS`; NSE uses `index=equities` only. Config `include_sme=false`.
- Confidence values: `VERIFIED`, `LLM_VERIFIED`, `UNVERIFIED`, `NO_TEXT`. Numbers are shown/emitted only for the first two.
- Plain-English flags and a final `What this means:` line in every numeric alert.
- Secrets only from `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, optional `GEMINI_API_KEY`); `.env` is git-ignored.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Restart mid-day** must not re-alert or flood: on startup, today's already-listed filings are marked seen without alerting (`backfill_on_start=false`). Test in Task 12.
2. **Exchange returns HTML / 403 / empty / non-JSON**: poller logs, backs off, keeps running; never raises out of the loop. Test in Task 9.
3. **Broken PDF** (404, >40 MB, corrupt bytes, zero pages): filing state `error`, no crash, no alert. Test in Task 12.
4. **Loss-making companies** (negative or zero base): growth % is `None` (never "-250 %"); turnaround flags instead. Test in Task 5.
5. **Telegram unreachable / 429**: message retried from the queue; the event row is still written. Test in Task 11.

## File Structure

```
pyproject.toml, config.example.toml, .env.example, README.md
resultagent/
  __init__.py
  __main__.py        CLI: run | replay | test-telegram | get-chat-id
  config.py          Settings dataclasses, load_settings()
  models.py          Filing, Extraction, Validation, Metric, Flag, Analysis, Confidence, Template
  numparse.py        tokenize_numbers(), detect_unit_divisor(), detect_period_end(), fy_label()
  extract.py         detect_template(), detect_statement(), extract_rows()
  validate.py        validate(), repair()
  analyse.py         analyse()
  fmt.py             format_flash(), format_headline(), fmt_cr(), fmt_pct()
  pdftext.py         PdfInfo, load_pdf(), find_result_pages(), vision_candidates(), render_jpeg()
  store.py           Store (SQLite)
  http.py            Http (AsyncSession wrapper, get_json/get_bytes/post_json, backoff)
  sources.py         NseSource, BseSource, ScripMaster, is_result_announcement()
  llm.py             LlmRouter, ModelSpec
  llm_tasks.py       llm_extract_vision(), llm_commentary(), NOT_FOUND
  telegram.py        Telegram (queue, send, edit)
  pipeline.py        Pipeline.process(filing)
  app.py             App (pollers, scheduler, heartbeat, replay)
deploy/resultagent.service, deploy/install.sh
docs/SETUP.md
tests/ (one test file per module) + tests/fixtures/{pages,api,pdf}/
```

---

### Task 1: Project scaffold, config, models

**Files:**
- Create: `pyproject.toml`, `config.example.toml`, `.env.example`, `resultagent/__init__.py`, `resultagent/config.py`, `resultagent/models.py`, `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (`models.py`, all `@dataclass(slots=True)`; enums are `StrEnum`):
  - `Template(StrEnum)`: `CORPORATE="corporate"`, `BANK="bank"`, `LENDER="lender"`
  - `Confidence(StrEnum)`: `VERIFIED`, `LLM_VERIFIED`, `UNVERIFIED`, `NO_TEXT`
  - `Filing(exchange: str, filing_id: str, isin: str | None, symbol: str, name: str, pdf_url: str, disseminated_at: datetime, subject: str, scrip_code: str | None = None)`; property `key -> str` = `isin or f"BSE:{scrip_code}"`
  - `Cell(value: float | None, anomaly: bool = False)`
  - `Extraction(template: Template, statement: str, period_end: date | None, rows: dict[str, list[Cell]], source: str, unit: str)` — `rows[key]` always length 3; `source` ∈ `rules|llm_vision`; `statement` ∈ `standalone|consolidated`; method `val(key, col) -> float | None`
  - `Validation(ok: bool, passed: list[str], failed: list[str], repaired: list[str])`
  - `Metric(name: str, value: float | None, yoy: float | None, qoq: float | None, kind: str = "cr")` (`kind` ∈ `cr|pct|eps|margin`)
  - `Flag(positive: bool, text: str)`
  - `Analysis(metrics: list[Metric], flags: list[Flag], score: int, verdict: str, summary: str)`
- Produces (`config.py`): `load_settings(config_path: Path, env_path: Path | None) -> Settings` with nested dataclasses `Settings(telegram: TelegramCfg, poll: PollCfg, filter: FilterCfg, llm: LlmCfg, paths: PathsCfg, scoring: dict[str, float], include_sme: bool, backfill_on_start: bool, webhook_url: str | None)`; `LlmCfg.models: list[ModelSpecCfg(name, provider, model, vision: bool, rpm: int, rpd: int)]`, `LlmCfg.commentary_reserve: float = 0.3`. Provider base URLs: `groq=https://api.groq.com/openai/v1`, `openrouter=https://openrouter.ai/api/v1`, `gemini=https://generativelanguage.googleapis.com/v1beta/openai`. A model whose provider key is missing in env is dropped at load.

- [ ] **Step 1:** Write `pyproject.toml` (package `resultagent`, deps per Global Constraints, `[project.optional-dependencies] dev = ["pytest>=8", "pytest-asyncio>=0.23"]`, `asyncio_mode = "auto"`), `config.example.toml` with every key and a comment (default LLM list: groq `openai/gpt-oss-120b`, `llama-3.3-70b-versatile`, `openai/gpt-oss-20b`, `llama-3.1-8b-instant` (text, rpm 30, rpd 1000); openrouter vision `google/gemma-4-31b-it:free`, `nvidia/nemotron-3.5-super-vl:free`, `google/gemma-4-26b-a4b-it:free` (rpm 20, rpd 50 — comment: 1000 after $10 credit); gemini block commented out), `.env.example`.
- [ ] **Step 2: Write failing tests** `tests/test_config.py`:
  - `test_loads_example_config` — `load_settings(Path("config.example.toml"), env_file_with_all_keys)` → `s.poll.fast_seconds == 10`, `s.poll.slow_seconds == 60`, `s.filter.enabled is False`, `s.include_sme is False`, `s.backfill_on_start is False`, `s.llm.commentary_reserve == 0.3`.
  - `test_models_without_key_are_dropped` — env without `OPENROUTER_API_KEY` → no model with `provider == "openrouter"`.
  - `test_missing_telegram_token_raises` — `ValueError` mentioning `TELEGRAM_BOT_TOKEN`.
- [ ] **Step 3:** Run `pytest tests/test_config.py -v` → FAIL (module missing).
- [ ] **Step 4:** Implement `config.py` (`tomllib`; `.env` parsed with a tiny `KEY=VALUE` reader, `os.environ` overrides) and `models.py`.
- [ ] **Step 5:** Run `pytest tests/test_config.py -v` → PASS.
- [ ] **Step 6:** Commit `feat: scaffold, config and models`.

### Task 2: Number, unit and period parsing

**Files:** Create `resultagent/numparse.py`; Test `tests/test_numparse.py`

**Interfaces:**
- Produces:
  - `tokenize_numbers(s: str) -> list[Cell]` — numeric tokens left→right from text that follows a row label.
  - `detect_unit_divisor(page_text: str) -> tuple[str, float]` — returns `(unit_name, divisor_to_crore)`: crore→1, lakh→100, million→10, thousand→10_000, rupees→1e7. Default `("crore", 1.0)` if nothing found.
  - `detect_period_end(page_text: str) -> date | None`
  - `fallback_period_end(filed: date) -> date` — last quarter end (Mar 31/Jun 30/Sep 30/Dec 31) strictly before `filed`.
  - `fy_label(period_end: date) -> str` — e.g. `date(2026,9,30)` → `"Q2 FY27"`, `date(2027,3,31)` → `"Q4 FY27"`, `date(2026,6,30)` → `"Q1 FY27"`, `date(2026,12,31)` → `"Q3 FY27"`.

Token rules (decide nothing else differently):
- Before tokenising, delete formula refs — any bracket group (opening `( { [`, closing `) } ]`, mixed allowed) that contains `+` or `-` and **no `.`** (e.g. `(3-4}`, `{13+14+15)`, `[1-11]`, `(VII-VIII]`, `(1+2)`) — and `\(refer note[^)]*\)` (case-insensitive), and the stray characters `| • ' " * ’ ‘ °`.
- Tokenising **stops at the first alphabetic word of ≥ 4 letters** (start of the next label on the same line — Rossari: `Exceptional items - - VII I Profit before tax [V-VI] 492.69 …` gives exceptional `[0.0, 0.0]`). Words without digits are never tokens.
- A dash token (`-`, `–`, `—`) standing alone → `Cell(0.0)`.
- Parentheses or braces around a number, in any mix (`(25.07)`, `{25.07)`, `(122,54)`, `(578.80`) → negative.
- Separators: if the token has both `,` and `.`, the last `.` is decimal and `,` are thousands. Only `,` and the part after the last comma is exactly 2 digits and there is a single comma (`961,79`) → decimal comma. Multiple `.` (`3.051.74`) → last is decimal, earlier ones thousands, `anomaly=True`. Doubled `..` (`222..76`) → single, `anomaly=True`.
- More than 2 decimals (`3.867`) → keep value, `anomaly=True`.
- An integer token without decimals when ≥1 other token on the same line has 2 decimals → `anomaly=True` (decimal-loss suspect, e.g. `2527`). Integers ≤ 2 digits in that situation are dropped (serial/note numbers like `(4)`).
- Tokens containing letters (`8B.08`, `o.n`, `1.n`) → `Cell(None, anomaly=True)`.
- `%` suffix stripped (`1.11%` → 1.11).

- [ ] **Step 1: Write failing tests** (`tests/test_numparse.py`), assert values with `pytest.approx`:
  - `test_plain_row`: `tokenize_numbers("1,142.03 1,019.42 1,117.04 2,161.45")` → values `[1142.03, 1019.42, 1117.04, 2161.45]`, no anomalies.
  - `test_brackets_braces_dash`: `"{25.07) (12.51) 43.61 - (122,54)"` → `[-25.07, -12.51, 43.61, 0.0, -122.54]`.
  - `test_decimal_comma`: `"79362,78 45139,20"` → `[79362.78, 45139.20]`.
  - `test_dotted_thousands`: `"3.051.74 2,856.07"` → `[3051.74 (anomaly), 2856.07]`; `"222..76 213.46"` → `[222.76 (anomaly), 213.46]`.
  - `test_three_decimals_flagged`: `"3.867 3.45 29.00"` → first `anomaly=True`.
  - `test_decimal_loss_flagged`: `"24.08 26.15 2527 50.21"` → third value 2527 `anomaly=True`.
  - `test_formula_refs_removed`: `"(3-4} 52,28 61.08 10.40"` → `[52.28, 61.08, 10.40]`; `"(4) 1,093.42 961,79 1,135.64"` → `[1093.42, 961.79, 1135.64]`; `"(Refer note 12) 13831.02 12821.60"` → `[13831.02, 12821.60]`.
  - `test_letters_none`: `"8B.08 0.91"` → `[None(anomaly), 0.91]`.
  - `test_stops_at_next_label`: `"- - VII I Profit before tax [V-VI] 492.69 476.72"` → `[0.0, 0.0]`.
  - `test_letter_formula_ignored`: `"(a)+(b)+(c)+(d) 83426.60 79362,78"` → `[83426.60, 79362.78]`.
  - `test_units`: fixture texts → hdfcbank `("crore",1)`, indiacem `("crore",1)`, rossari `("million",10)`, kross `("million",10)`, bajajhfl `("crore",1)` (its line is `(f incrore)` — match `crore` anywhere in a parenthesised/“in” unit line, OCR-tolerant), `"(Rs. in lakhs)"` → `("lakh",100)`.
  - `test_period`: hdfcbank fixture → `date(2026,9,30)`; rossari → `date(2026,9,30)` ("30TH SEPTEMBER, 2026"); bajajhfl → `date(2026,9,30)` ("ended 30 September 2026"); `"quarter ended 31.12.2026"` → `date(2026,12,31)`; `"quarter ended 30-Jun-2026"` → `date(2026,6,30)`.
  - `test_fallback_period`: `fallback_period_end(date(2026,10,17))` → `date(2026,9,30)`; `date(2026,7,1)` → `date(2026,6,30)`.
  - `test_fy_label` — the four cases in Interfaces.
- [ ] **Step 2:** Run `pytest tests/test_numparse.py -v` → FAIL.
- [ ] **Step 3:** Implement `numparse.py`. `detect_period_end` searches `quarter[^\n]{0,60}?ended[^\n]{0,10}?(<date>)` case-insensitive, with date forms `September 30, 2026`, `30th September 2026`, `30 September, 2026`, `30.09.2026`, `30-09-2026`, `30-Sep-2026`; only accepts dates that are quarter ends.
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5:** Commit `feat: number, unit and period parsing`.

### Task 3: Rule-based table extraction

**Files:** Create `resultagent/extract.py`; Test `tests/test_extract.py`; fixtures already in `tests/fixtures/pages/` (`hdfcbank_std.txt`, `rossari_cons.txt`, `indiacem_cons.txt`, `kross_std.txt`, `idbi_std.txt`, `bajajhfl_std.txt`).

**Interfaces:**
- Consumes: `tokenize_numbers`, `detect_unit_divisor`, `detect_period_end` (Task 2); `Extraction`, `Cell`, `Template` (Task 1).
- Produces:
  - `ROW_KEYS: dict[Template, tuple[str, ...]]`:
    - CORPORATE: `revenue, other_income, total_income, total_expenses, finance_cost, depreciation, pbt_before_exceptional, associates, exceptional, pbt, tax, pat, pat_owners, eps`
    - BANK: `interest_earned, other_income, total_income, interest_expended, opex, ppop, provisions, exceptional, pbt, tax, pat, eps, gnpa_pct, nnpa_pct`
    - LENDER: `interest_income, revenue, other_income, total_income, finance_cost, impairment, total_expenses, pbt_before_exceptional, exceptional, pbt, tax, pat, eps`
  - `detect_template(page_text: str) -> Template | None` — BANK if `interest earned` and `interest expended` (OCR-tolerant `expend`); LENDER if `interest income` and (`impairment on financial instruments` or `finance cost`) and not BANK; CORPORATE if `revenue from operations` or `total income`; else `None`.
  - `detect_statement(page_text: str) -> str` — `consolidated` if `consolidated` appears in the first 800 chars, else `standalone`.
  - `extract_rows(page_text: str) -> Extraction | None` — `None` if no template. Amount rows divided by the unit divisor (not `eps`, `gnpa_pct`, `nnpa_pct`). `source="rules"`.

Row matching (the implementer writes the regex lists; these decisions are fixed):
- Labels may match **anywhere in a line** (two rows can share one text line); numbers are taken from text after the match, via `tokenize_numbers` (which stops at the next label word).
- Each key has an ordered list of label regexes; the **first matching line** in reading order wins for that key, except `pbt_before_exceptional` takes the first line matching `profit.{0,40}before.{0,60}exceptional` (Rossari's first such line = 488.00, before JV share) and `associates` matches `share of (net )?profit.{0,30}(associate|joint venture)`.
- `revenue` for CORPORATE matches `revenue from operations` but not a line that is only the heading `(a) Revenue from operations` with <1 numeric token; LENDER `revenue` matches `total revenue from operations`.
- Numbers are taken from text **after** the label match. If the label line yields <3 cells, append cells from up to the next 3 lines that contain ≥3 numeric tokens and no letters other than units (HDFC EPS case). Keep the first 3 cells.
- `tax`: if no total-tax row with numbers, sum `current tax` + `deferred tax` (+ `earlier years`) rows per column.
- `pat`: CORPORATE prefers `net profit.{0,40}after.{0,40}(minority|non.controlling|associates)` then `profit (after tax|for the period|/ ?\(loss\) for the period)`, excluding lines containing `continuing operations` only if a later total exists; `pat_owners` matches `owners of the (company|parent)` (Rossari: numbers on the same line).
- `ppop` matches `operating profit before provisions`; `provisions` matches `provisions \(other than tax\)`; `gnpa_pct` matches `% of gross npa`; `nnpa_pct` matches `% of net npa`.

- [ ] **Step 1: Write failing tests** (`tests/test_extract.py`, values via `ex.val(key, col)`, `approx(abs=0.01)`):
  - `test_hdfc_bank` (extraction is **faithful to the text layer**, which has OCR slips: `total_income` col0 97267.62 and `pat` col0 19609.96 are as printed; XBRL truth is 97257.62 and 19509.96): template BANK, statement standalone, period `2026-09-30`; `interest_earned` `[83426.60, 79362.78, 76690.70]`; `interest_expended` `[48807.42, 45828.83, 45139.20]`; `other_income` `[13831.02, 12821.60, 14350.02]`; `total_income` `[97267.62, 92184.38, 91040.72]`; `opex` `[18966.78, 18187.49, 17977.92]`; `ppop` `[29483.42, 28168.06, 27923.60]`; `provisions` `[3573.62, 3059.76, 3500.53]`; `pbt` `[25909.80, 26108.30, 24423.07]`; `tax` `[6399.84, 6048.58, 5781.79]`; `pat` `[19609.96, 19069.72, 18641.28]`; `eps` `[12.66, 12.38, 12.14]`; `gnpa_pct` `[1.11, 1.17, 1.24]`; `nnpa_pct` `[0.37, 0.41, 0.42]`.
  - `test_rossari_corporate_million_to_crore`: CORPORATE, consolidated; `revenue` `[697.560, 697.200, 586.107]`; `other_income` `[0.599, 3.187, 1.604]`; `total_income` `[698.159, 700.387, 587.711]`; `total_expenses` `[649.359, 653.172, 538.507]`; `finance_cost` `[8.677, 10.977, 6.091]`; `depreciation` `[30.096, 25.615, 18.233]`; `pbt_before_exceptional` `[48.800, 47.215, 49.204]`; `associates` `[0.469, 0.457, 0.468]`; `pbt` `[49.269, 47.672, 49.672]`; `tax` `[10.362, 12.577, 12.796]`; `pat` `[38.907, 35.095, 36.876]`; `eps` `[7.02, 6.34, 6.66]`.
  - `test_indiacem_ocr_values`: CORPORATE, consolidated; `revenue` `[1142.03, 1019.42, 1117.04]`; `other_income` col0 `3.867` with `anomaly=True`; `total_income` `[1145.70, 1022.87, 1146.04]`; `total_expenses` `[1093.42, 961.79, 1135.64]`; `exceptional` `[0.0, 25.28, 6.01]`; `pbt` `[52.28, 35.80, 4.39]`; `tax` `[13.46, 8.35, -4.42]` (sum of current+deferred); `pat` `[38.82, 26.85, 8.81]`.
  - `test_bajajhfl_lender`: LENDER, standalone; `interest_income` `[3051.74, 2856.07, 2614.44]`; `revenue` `[3250.57, 3063.02, 2748.04]`; `other_income` `[0.52, 0.03, 0.15]`; `total_income` `[3251.09, 3063.05, 2748.19]`; `finance_cost` `[1996.65, 1888.49, 1657.96]`; `impairment` `[34.78, 16.10, 42.85]`; `total_expenses` `[2271.36, 2134.31, 1915.53]`; `pbt` `[979.73, 928.74, 832.66]`; `tax` `[222.76, 213.46, 189.70]`; `pat` `[756.97, 715.28, 642.96]`.
  - `test_idbi_column_layout_returns_garbage_not_crash`: `extract_rows(idbi)` returns an `Extraction` or `None` without raising (validation in Task 4 rejects it).
  - `test_no_template`: cover-letter text (`"Dear Sir/Madam, Outcome of the Board Meeting ..."`) → `None`.
- [ ] **Step 2:** Run `pytest tests/test_extract.py -v` → FAIL.
- [ ] **Step 3:** Implement `extract.py`.
- [ ] **Step 4:** Run tests → PASS. If a fixture value can only be reached by a fixture-specific hack, stop and report instead of special-casing.
- [ ] **Step 5:** Commit `feat: rule-based results table extraction`.

### Task 4: Arithmetic validation and single-cell repair

**Files:** Create `resultagent/validate.py`; Test `tests/test_validate.py`

**Interfaces:**
- Consumes: `Extraction`, `Validation` (Task 1); `extract_rows` (Task 3) in tests.
- Produces: `validate(ex: Extraction) -> Validation` (pure; calls `repair` internally and mutates `ex.rows` only for repaired cells).

Identities per column `c` (term missing ⇒ that identity is skipped for that column; `exceptional`, `associates` missing ⇒ 0). Tolerance `tol(x) = max(0.05, 0.0001*abs(x))` in **original units** — compute it as `max(0.05/div, 0.0001*abs(x))` on crore values using the divisor implied by `ex.unit`.
- CORPORATE: `C1 revenue+other_income≈total_income`; `C2 total_income−total_expenses (+associates allowed) ≈ pbt_before_exceptional`; `C3 pbt_before_exceptional (+associates) ± exceptional ≈ pbt` (either sign of exceptional; with or without associates); `C4 pbt−tax ≈ pat` (or `pbt−tax+associates ≈ pat`).
- BANK: `B1 interest_earned+other_income≈total_income`; `B2 total_income−interest_expended−opex≈ppop`; `B3 ppop−provisions±exceptional≈pbt`; `B4 pbt−tax≈pat`.
- LENDER: `L1 revenue+other_income≈total_income`; `L2 total_income−total_expenses≈pbt_before_exceptional`; `L3` as C3; `L4` as C4.
- `ok` = for column 0: (C1|B1|L1) and (C4|B4|L4) pass and at least one of the middle identities passes; for columns 1 and 2 when present: first and last identities pass. Failed names recorded as e.g. `"C1@0"`.
- Repair (per column, at most one cell per column): for each term of each failing identity, solve that identity for the term and accept the solution only if (a) **all** identities in that column then pass, and (b) the cell had `anomaly=True` **or** the cell takes part in ≥ 2 identities (so two independent equations agree). Set `Cell(solved, anomaly=False)`, append e.g. `"other_income@0"` to `repaired`. Never repair `pat` unless (a) and (b) both hold. Also, a `finance_cost`/`depreciation` cell with `anomaly=True` whose value/100 lies within 0.33–3× of the same row's other columns is replaced by value/100 (decimal loss) and recorded in `repaired`.
- Sanity (fail ⇒ `ok=False`): `pat` col0 present; `gnpa_pct`/`nnpa_pct` if present must satisfy `0 ≤ nnpa ≤ gnpa ≤ 40` else those cells set to `None` (not a failure); `eps` cells dropped (`None`) if `pat/eps` for col0 vs col2 differ by >25 % (share-count sanity).

- [ ] **Step 1: Write failing tests:**
  - `test_hdfc_text_layer_rejected` — `v = validate(extract_rows(hdfc))`: `"total_income@0" in v.repaired` (→ 97257.62, solved from B1 and confirmed by B2) **and** `v.ok is False` because `B4@0` fails (PAT typo cannot be repaired from one equation). This is the reason the vision path exists.
  - `test_hdfc_truth_ok` — the synthetic `hdfc_truth()` Extraction (see conftest below) → ok True, `repaired == []`.
  - `test_rossari_ok_with_associates` — ok True.
  - `test_indiacem_repairs_but_rejects` — `"other_income@0" in v.repaired` and `ex.val("other_income",0) == approx(3.67, abs=0.01)`; `"finance_cost@2" in v.repaired` (2527 → 25.27); `v.ok is False` because `C4@1` fails (deferred tax printed 8.35, truth 8.95).
  - `test_bajajhfl_ok` — ok True.
  - `test_kross_rejected` — ok False.
  - `test_idbi_rejected` — `extract_rows` → `None` or `validate(...).ok is False`.
  - `test_missing_pat_fails` — synthetic Extraction without `pat` → ok False.
  - `test_eps_dropped_on_share_mismatch` — synthetic with pat `[100,90,80]`, eps `[10,9,80]` → eps cells all `None`, ok unaffected.
- Add to `tests/conftest.py`: `hdfc_truth() -> Extraction` (BANK, standalone, 2026-09-30, source `rules`, unit `crore`) with col0 = XBRL truth `interest_earned 83426.60, other_income 13831.02, total_income 97257.62, interest_expended 48807.42, opex 18966.78, ppop 29483.42, provisions 3573.62, exceptional 0, pbt 25909.80, tax 6399.84, pat 19509.96, eps 12.66, gnpa_pct 1.11, nnpa_pct 0.37`; col1 `79362.78, 12821.60, 92184.38, 45828.83, 18187.49, 28168.06, 3059.76, 0, 25108.30, 6048.58, 19059.72, 12.38, 1.17, 0.41`; col2 `76690.70, 14350.02, 91040.72, 45139.20, 17977.92, 27923.60, 3500.53, 0, 24423.07, 5781.79, 18641.28, 12.14, 1.24, 0.42` (same key order as listed).
- [ ] **Step 2:** Run `pytest tests/test_validate.py -v` → FAIL.
- [ ] **Step 3:** Implement `validate.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: arithmetic validation with single-cell repair`.

### Task 5: Analysis (metrics, flags, score, plain summary)

**Files:** Create `resultagent/analyse.py`; Test `tests/test_analyse.py`

**Interfaces:**
- Consumes: `Extraction`, `Analysis`, `Metric`, `Flag`, `Template` (Task 1); `Settings.scoring` (weights dict, keys below).
- Produces: `analyse(ex: Extraction, weights: dict[str, float] | None = None) -> Analysis`; `growth(cur: float | None, base: float | None) -> float | None` (percent; `None` if either is None or `base <= 0`).

Metrics (order = display order):
- CORPORATE: `Revenue`, `EBITDA` (= `revenue − (total_expenses − finance_cost − depreciation)`), `EBITDA margin` (`kind="margin"`, value %, yoy/qoq = change in **bps**), `PAT` (prefer `pat_owners` else `pat`), `EPS` (`kind="eps"`).
- BANK: `NII` (= interest_earned − interest_expended), `Other income`, `PPOP`, `Provisions`, `PAT`, `GNPA` and `NNPA` (`kind="pct"`, yoy/qoq = change in percentage points).
- LENDER: `NII` (= interest_income − finance_cost), `Total revenue`, `Impairment`, `PAT`, `EPS`.

Score: start `5`, add weights (defaults below, overridable via `weights`), clamp 1–10. `growth_key` = Revenue (CORPORATE), NII (BANK/LENDER).
- `rev_yoy_gt20 +2`, `rev_yoy_gt10 +1`, `rev_yoy_lt0 −1`, `rev_yoy_ltm10 −2` (mutually exclusive, strongest applies)
- `pat_yoy_gt25 +2`, `pat_yoy_gt10 +1`, `pat_yoy_lt0 −1`, `pat_yoy_ltm20 −2`
- `margin_up100 +1`, `margin_down100 −1` (CORPORATE EBITDA margin YoY bps)
- `turnaround +2` (PAT col2 ≤ 0 and col0 > 0), `to_loss −3` (PAT col2 > 0 and col0 ≤ 0)
- `other_income_heavy −1` (CORPORATE/LENDER: other_income > 25 % of pbt, pbt > 0)
- `exceptional_heavy −1` (|exceptional| > 10 % of |pbt|)
- `provisions_outpace −1` (BANK/LENDER: provisions/impairment YoY growth − PPOP/NII YoY growth > 20 pts)
- `gnpa_down +1`, `gnpa_up −1` (BANK, QoQ change ≥ 5 bps)
- `qoq_rev_drop −1` (growth_key QoQ < −10 %)
- Verdict: ≥8 `STRONG`, 6–7 `GOOD`, 5 `MIXED`, 3–4 `WEAK`, ≤2 `POOR`.

Flags (exact plain-English copy, `{x}` formatted to 0 decimals):
- `✅` `Sales grew {x}% vs last year` (rev YoY > 15) / `⚠️` `Sales fell {x}% vs last year` (< 0)
- `✅` `Profit grew {x}% vs last year` (> 15) / `⚠️` `Profit fell {x}% vs last year` (< 0)
- `✅` `Operating margin improved by {x} bps — the core business is getting more profitable` / `⚠️` `Operating margin shrank by {x} bps — costs are rising faster than sales` (|bps| > 100)
- `⚠️` `Other income is {x}% of profit — part of the profit is not from the core business`
- `⚠️` `One-time (exceptional) items of ₹{x} Cr affect profit — check before trusting the growth`
- `✅` `Turned from loss to profit vs last year` / `⚠️` `Slipped into a loss vs last year`
- `⚠️` `Bad-loan provisions are growing faster than operating profit` (provisions_outpace)
- `✅` `Bad loans (GNPA) improved to {g:.2f}%` / `⚠️` `Bad loans (GNPA) worsened to {g:.2f}%`
- `⚠️` `Tax rate looks unusual ({x}%) — profit may not repeat` (effective tax < 15 or > 35, pbt > 0)
Summary line (`summary`, ≤ 140 chars) by verdict: STRONG `Strong quarter: both sales and profit are growing well.`; GOOD `Good quarter: growth is healthy, with minor watch-points.`; MIXED `Mixed quarter: some numbers improved, others weakened.`; WEAK `Weak quarter: growth is slowing or quality is poor.`; POOR `Poor quarter: sales and/or profit fell sharply.` For BANK/LENDER replace "sales" with "core income".

- [ ] **Step 1: Write failing tests:**
  - `test_growth_negative_base_is_none` — `growth(10, -5) is None`, `growth(10, 0) is None`, `growth(12, 10) == approx(20.0)`.
  - `test_rossari` — Revenue yoy `approx(19.02, abs=0.05)`, qoq `approx(0.05, abs=0.05)`; EBITDA value `approx(86.974, abs=0.01)`; margin `approx(12.47, abs=0.02)`, margin yoy bps `approx(20, abs=3)`; PAT yoy `approx(5.51, abs=0.05)`; `score == 6` (5 + rev_yoy_gt10), `verdict == "GOOD"`.
  - `test_hdfc_bank` (on `hdfc_truth()`) — NII value `approx(34619.18, abs=0.01)`, yoy `approx(9.72, abs=0.05)`, qoq `approx(3.24, abs=0.05)`; PAT yoy `approx(4.66, abs=0.05)`; GNPA yoy `approx(-0.13, abs=0.001)` (pp); a flag with `positive=True` and text starting `Bad loans (GNPA) improved to 1.11%` (QoQ −6 bps → `gnpa_down`); `score == 6`, `verdict == "GOOD"`.
  - `test_turnaround` — synthetic CORPORATE: PAT `[10, 5, -3]` → flag `Turned from loss to profit vs last year`, PAT yoy `None`, score includes +2.
  - `test_to_loss` — PAT `[-4, 5, 6]` → `Slipped into a loss vs last year`, verdict in (`WEAK`, `POOR`).
  - `test_weights_override` — `analyse(ex, {"turnaround": 0})` changes score vs default.
- [ ] **Step 2:** Run `pytest tests/test_analyse.py -v` → FAIL.
- [ ] **Step 3:** Implement `analyse.py` (weights default dict at module top `DEFAULT_WEIGHTS`).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: deterministic results analysis and scoring`.

### Task 6: Telegram message formatting

**Files:** Create `resultagent/fmt.py`; Test `tests/test_fmt.py`

**Interfaces:**
- Consumes: `Filing`, `Extraction`, `Analysis`, `Confidence` (Task 1); `fy_label` (Task 2).
- Produces:
  - `fmt_cr(v: float | None) -> str` — `None`→`n/a`; Indian grouping; |v| ≥ 1000 → 0 decimals, ≥ 10 → 1 decimal, else 2; prefix `₹`, suffix ` Cr`; negatives `−₹12.5 Cr` (U+2212).
  - `fmt_pct(v: float | None, kind: str = "pct") -> str` — `None`→`n/a`; growth `+9.7%`/`−3.2%`; `kind="bps"` → `+20 bps`; `kind="pp"` → `−0.13 pp`.
  - `format_flash(f: Filing, ex: Extraction, a: Analysis, conf: Confidence, alerted_at: datetime) -> str` (Telegram HTML).
  - `format_headline(f: Filing, reason: str, alerted_at: datetime) -> str`.
  - `append_ai_view(html: str, bullets: list[str]) -> str`.

Layout (HTML, escape all dynamic text with `html.escape`): line 1 `{emoji} <b>{symbol}</b> · {name} · {fy_label} ({Statement}) · {Template label}`; line 2 `Verdict: <b>{verdict}</b> · Score {score}/10 · {✔ verified | ✔ AI-read, verified}`; `<pre>` block with one metric per line `{name:<11} {value:>13}  YoY {yoy:>7}  QoQ {qoq:>7}`; flags (`✅`/`⚠️` + text); `<i>What this means:</i> {summary}`; footer `📄 <a href="{pdf_url}">PDF</a> · {exchange} {HH:MM:SS} · alert {HH:MM:SS} ({n} s)`. Emoji: STRONG/GOOD 🟢, MIXED 🟡, WEAK/POOR 🔴. Headline: `⚪ <b>{symbol}</b> · {name} — results filed` / `{reason}` / footer. Whole message ≤ 1,500 chars (truncate flags first).

- [ ] **Step 1: Write failing tests:** `test_fmt_cr` (`fmt_cr(112400)=="₹1,12,400 Cr"`, `fmt_cr(34619.18)=="₹34,619 Cr"`, `fmt_cr(86.974)=="₹87.0 Cr"`, `fmt_cr(3.67)=="₹3.67 Cr"`, `fmt_cr(-12.5)=="−₹12.5 Cr"`, `fmt_cr(None)=="n/a"`); `test_fmt_pct` (`fmt_pct(9.72)=="+9.7%"`, `fmt_pct(-3.24)=="−3.2%"`, `fmt_pct(20,"bps")=="+20 bps"`); `test_flash_contains` (`hdfc_truth()` through analyse: output contains `<b>HDFCBANK</b>`, `Q2 FY27 (Standalone)`, `NII`, `₹34,619 Cr`, `What this means:`, `href="https://`, and `len(out) <= 1500`); `test_escapes_html` (name `"A&B <Ltd>"` → `A&amp;B &lt;Ltd&gt;`); `test_headline`.
- [ ] **Step 2:** Run `pytest tests/test_fmt.py -v` → FAIL.
- [ ] **Step 3:** Implement `fmt.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: telegram message formatting`.

### Task 7: PDF loading, result-page finding, page rendering

**Files:** Create `resultagent/pdftext.py`, `tests/fixtures/pdf/make_fixtures.py` (dev script); Test `tests/test_pdftext.py`; fixture PDFs `tests/fixtures/pdf/hdfc_page.pdf` and `tests/fixtures/pdf/rossari_page.pdf` (single pages extracted with `pypdfium2` `PdfDocument.new()` + `import_pages` from the real PDFs linked in the 17-10-2026 NSE announcements, `desc == "Outcome of Board Meeting"`, for `HDFCBANK` (page index 1) and `ROSSARI` (page index 5)), and a synthetic image-only PDF generated **in the test** with Pillow (`Image.new("L",(1240,1754),255)` + `ImageDraw.text`, `save(..., "PDF")`).

**Interfaces:**
- Consumes: `detect_template`, `detect_statement`, `extract_rows` (Task 3).
- Produces:
  - `@dataclass PdfInfo(pages: list[str], doc: pdfium.PdfDocument)`; `load_pdf(data: bytes) -> PdfInfo` (raises `ValueError("bad pdf")` on corrupt/zero pages; reads text of at most first 30 pages).
  - `find_result_pages(info: PdfInfo) -> list[tuple[int, Template, str]]` — pages where `detect_template` is not None **and** a revenue/interest/total-income label line has ≥3 numeric tokens; tuples `(page_index, template, statement)`.
  - `pick_page(cands, prefer_bank_standalone=True) -> tuple[int, Template, str] | None` — non-bank: consolidated first; bank: standalone first; else first.
  - `vision_candidates(info: PdfInfo, limit: int = 2) -> list[int]` — page indices among the first 8 with `< 200` non-space text chars, in order.
  - `render_jpeg(info: PdfInfo, index: int, dpi: int = 150) -> bytes` — grayscale JPEG quality 70, longest side ≤ 2000 px.
  - `close(info)`.

- [ ] **Step 1:** Write `make_fixtures.py` and produce both page PDFs; commit them (each < 500 KB).
- [ ] **Step 2: Write failing tests:** `test_load_and_find_hdfc` (`find_result_pages` → `[(0, Template.BANK, "standalone")]`); `test_find_rossari` (→ `[(0, Template.CORPORATE, "consolidated")]`); `test_bad_pdf_raises` (`load_pdf(b"not a pdf")` → `ValueError`); `test_image_only_pdf_vision_candidates` (3-page Pillow PDF → `vision_candidates == [0, 1]`, `find_result_pages == []`); `test_render_jpeg` (bytes start with `b"\xff\xd8"`, `len < 600_000`).
- [ ] **Step 3:** Run `pytest tests/test_pdftext.py -v` → FAIL.
- [ ] **Step 4:** Implement `pdftext.py`.
- [ ] **Step 5:** Run → PASS.
- [ ] **Step 6:** Commit `feat: pdf text, result page detection and rendering`.

### Task 8: SQLite store

**Files:** Create `resultagent/store.py`; Test `tests/test_store.py`

**Interfaces:**
- Produces `class Store` (sync sqlite3, WAL, `check_same_thread=False`, one connection; all methods short):
  - `Store(path: Path)`; `close()`
  - `seen(exchange: str, filing_id: str) -> bool`; `mark(exchange, filing_id, key: str, state: str)` (`state` ∈ `seen|skipped|done|no_table|error|unverified`)
  - `alert_today_other_exchange(key: str, exchange: str, ist_date: date) -> bool`
  - `alert_exists(key: str, period_end: date, statement: str) -> bool`; `alert_statements(key, period_end) -> set[str]`
  - `save_alert(key, period_end, statement, exchange, filing_id, message_id: int | None, ist_date: date)`
  - `add_event(payload: dict) -> int` (stores JSON; columns `id, created_at, key, symbol, period_end, confidence, json`)
  - `llm_usage(day: date, model: str) -> tuple[int, int]`; `add_llm_usage(day, model, requests: int, tokens: int)`
  - `upsert_scrips(rows: list[dict])`; `scrip_by_code(code: str) -> dict | None`; `scrip_by_isin(isin: str) -> dict | None` (dict keys `code, isin, symbol, name, mcap_cr, grp`)
  - `stats_for_day(ist_date) -> dict` (`alerts`, `unverified`, `errors`) for heartbeat.

- [ ] **Step 1: Write failing tests** (tmp_path db): round-trip each method; `test_alert_today_other_exchange` (saved via NSE → True for BSE, False for NSE); `test_reopen_persists` (close + reopen keeps `seen`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (schema created in `__init__` with `CREATE TABLE IF NOT EXISTS`). **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: sqlite store`.

### Task 9: HTTP wrapper and exchange sources

**Files:** Create `resultagent/http.py`, `resultagent/sources.py`; Test `tests/test_sources.py`; fixtures `tests/fixtures/api/nse_announcements.json`, `bse_results.json`, `bse_scrips.json` (already recorded).

**Interfaces:**
- Produces (`http.py`): `class Http` with `async get_json(url, *, headers=None, timeout=20) -> Any`, `async get_bytes(url, *, max_bytes: int, timeout=30, headers=None) -> bytes` (streams; raises `TooLarge`), `async post_json(url, payload, *, headers=None, timeout=30) -> tuple[int, dict, dict]` (status, json, response headers), `async close()`. Raises `HttpError(status, body_snippet)` for non-2xx or non-JSON. Constructor takes an injectable `session` for tests.
- Produces (`sources.py`):
  - `is_result_announcement(exchange: str, row: dict) -> bool` — BSE: `SUBCATNAME == "Financial Results"`; NSE: `desc` in {`Outcome of Board Meeting`, `Financial Result Updates`, `Financial Results`} and `re.search(r"financial\s+results?", attchmntText, re.I)`.
  - `class NseSource(http)`: `async fetch(day: date) -> list[Filing]` → `GET https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={dd-mm-yyyy}&to_date={dd-mm-yyyy}` with `Referer: https://www.nseindia.com/`; warms `https://www.nseindia.com/` once per 10 min and after 401/403 (warm-up errors ignored). Filing: `filing_id=seq_id`, `isin=sm_isin`, `symbol`, `name=sm_name`, `pdf_url=attchmntFile`, `disseminated_at` from `exchdisstime` (`%d-%b-%Y %H:%M:%S`, IST), `subject=attchmntText`.
  - `class BseSource(http, store)`: `async fetch(day: date) -> list[Filing]` → pages 1..N (stop when `RN` reaches `Table1[0].ROWCNT` or page empty; max 5) of `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}&strCat=Result&strPrevDate={yyyymmdd}&strScrip=&strSearch=P&strToDate={yyyymmdd}&strType=C&subcategory=-1` with `Referer`/`Origin` `https://www.bseindia.com`; `filing_id=NEWSID`, `scrip_code=str(SCRIP_CD)`, `isin` and `symbol` from `store.scrip_by_code`, `name=SLONGNAME`, `pdf_url=https://www.bseindia.com/xml-data/corpfiling/AttachLive/{ATTACHMENTNAME}`, `disseminated_at` from `DissemDT` (ISO, IST), `subject=NEWSSUB`. Skips SME groups unless `include_sme`.
  - `class ScripMaster(http, store)`: `async refresh() -> int` → `ListofScripData` URL (spec §3), maps `SCRIP_CD, ISIN_NUMBER, scrip_id, Scrip_Name, Mktcap, GROUP` into `store.upsert_scrips`.
- [ ] **Step 1: Write failing tests** with a fake `Http` returning fixture JSON: `test_nse_parses_only_results` (every Filing has `pdf_url` ending `.pdf` and `is_result_announcement` true; non-result rows excluded; HDFCBANK row present with `isin == "INE040A01034"`); `test_bse_maps_isin_and_url` (after `ScripMaster.refresh` from `bse_scrips.json`, BSE filing for `532174` has `isin` of ICICI and `pdf_url` starts with `https://www.bseindia.com/xml-data/corpfiling/AttachLive/`); `test_bse_skips_sme` (filing whose scrip group is `M` excluded); `test_nse_html_response_raises_httperror` (fake returns HTML → `HttpError`, and `NseSource` marks a re-warm).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: exchange sources and http wrapper`.

### Task 10: LLM router and LLM tasks

**Files:** Create `resultagent/llm.py`, `resultagent/llm_tasks.py`; Test `tests/test_llm.py`

**Interfaces:**
- Consumes: `Http.post_json` (Task 9), `Store.llm_usage/add_llm_usage` (Task 8), `LlmCfg` (Task 1), `Extraction/Cell/Template`, `Analysis` (Task 1), `ROW_KEYS` (Task 3).
- Produces (`llm.py`): `class LlmRouter(http, store, cfg, env)`: `async complete(messages: list[dict], *, vision: bool, purpose: str, timeout: float) -> str | None`. Picks the first model with `vision` capability if needed, whose today's requests < `rpd` (for `purpose == "commentary"`: < `rpd * (1 - commentary_reserve)`), whose last-minute requests < `rpm`, and not in cooldown. POSTs `{base}/chat/completions` with `{"model", "messages", "temperature": 0, "response_format": {"type": "json_object"}, "max_tokens": 1200}`, `Authorization: Bearer`. On 429/5xx/timeout: cooldown = `retry-after` header seconds or 60 s, try next model, all within the single `timeout` budget. Records usage (`usage.total_tokens`). Returns `choices[0].message.content` or `None`.
- Produces (`llm_tasks.py`):
  - `async llm_extract_vision(router, jpegs: list[bytes], timeout: float) -> Extraction | None` — messages with `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}`. Prompt includes the exact row keys of `ROW_KEYS` per template and instructs: values exactly as printed (no unit conversion), negative for brackets, `null` if absent, 3 columns = current / previous / year-ago **quarter** (ignore half-year/nine-month/year columns), pick consolidated for non-banks and standalone for banks, and return `{"found": false}` if no P&L table. Response JSON: `{"found": true, "template", "statement", "unit" (crore|lakh|million|thousand|rupees), "period_end" (YYYY-MM-DD), "rows": {key: [c0, c1, c2]}}` → `Extraction(source="llm_vision")` with the unit divisor applied (same mapping as `detect_unit_divisor`). `{"found": false}` → returns `NOT_FOUND` sentinel (module constant) so the pipeline can tell "no table" from "failed".
  - `async llm_commentary(router, symbol: str, a: Analysis, timeout: float) -> list[str] | None` — sends only metrics/flags (no PDF), asks `{"bullets": [..3-4 short plain-English points, ≤ 110 chars each]}`; `purpose="commentary"`.
  - Malformed JSON / wrong shapes → `None` (never raise).
- [ ] **Step 1: Write failing tests** with a fake Http: `test_router_falls_back_on_429` (first model 429 with `retry-after: 30` → second model used; first in cooldown); `test_router_respects_rpd` (usage at rpd → skipped); `test_commentary_reserve` (usage at 75 % of rpd → `purpose="commentary"` gets `None` when only that model exists, `purpose="extract"` succeeds); `test_vision_only_vision_models`; `test_extract_vision_parses_json` (fake returns HDFC truth JSON in `crore` → `Extraction` with `source == "llm_vision"`, `val("pat",0) == 19509.96`; in `million` → values ÷ 10); `test_vision_not_found_sentinel`; `test_bad_json_returns_none`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: free-tier llm router, extraction and commentary`.

### Task 11: Telegram client with queue

**Files:** Create `resultagent/telegram.py`; Test `tests/test_telegram.py`

**Interfaces:**
- Consumes: `Http.post_json` (Task 9).
- Produces `class Telegram(http, token: str, chat_id: str)`:
  - `async send(html: str) -> int | None` — enqueues and awaits delivery; returns `message_id` or `None` after 5 failed attempts (backoff 2,4,8,16,32 s; on 429 sleep `parameters.retry_after`).
  - `async edit(message_id: int, html: str) -> bool`.
  - `async run()` — the queue worker enforcing ≤1/s, ≤18/min; started by App.
  - `async get_chat_ids() -> list[tuple[str, str]]` (from `getUpdates`, for setup).
  - API base `https://api.telegram.org/bot{token}/`; `sendMessage` with `parse_mode="HTML"`, `disable_web_page_preview=True`.
- [ ] **Step 1: Write failing tests** (fake Http, fake clock via injectable `sleep`): `test_send_returns_message_id`; `test_retry_after_429` (first call 429 `retry_after=3` → slept 3, second succeeds); `test_rate_limit_spacing` (3 sends → sleeps ≥ 1 s between); `test_gives_up_returns_none` (always 500 → `None` after 5 attempts).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: telegram client with rate-limited queue`.

### Task 12: Pipeline orchestration

**Files:** Create `resultagent/pipeline.py`; Test `tests/test_pipeline.py`

**Interfaces:**
- Consumes: everything above.
- Produces `class Pipeline(settings, store, http, router: LlmRouter | None, telegram, now: Callable[[], datetime])`:
  - `async process(f: Filing) -> str` returning the final state (`skipped|done|no_table|error|unverified`), never raising.
  - `pending_commentary: set[asyncio.Task]`.

Order (spec §5, §10a): `seen?`→return `skipped`; `mark seen`; filter (`settings.filter`: `watchlist` symbols/ISINs, `min_mcap_cr` via `store.scrip_by_isin`, `unknown_mcap_pass`) → `skipped`; per-key `asyncio.Lock`; `alert_today_other_exchange` → `skipped`; download (`Http.get_bytes`, 40 MB, semaphore 3; errors → `error`); `load_pdf` (ValueError → `error`); rules: `pick_page(find_result_pages)` → `extract_rows` → `validate` → `VERIFIED`; else within one 25 s `asyncio.timeout`: pages = [the picked page] if one was found, else `vision_candidates(info)`; `render_jpeg` each → `llm_extract_vision` → `validate`; ok → `LLM_VERIFIED`. `NOT_FOUND` from vision, or no picked page and no vision candidates → `no_table` (no alert). Not verified → headline alert (`format_headline`, reason `"Numbers could not be read reliably — open the PDF"` or `"Scanned PDF — open the PDF"` → `NO_TEXT`) → state `unverified`. Period: `ex.period_end or fallback_period_end(f.disseminated_at.date())`. Final dedupe: `alert_exists(key, period, statement)` → `skipped`; if existing statements = {standalone} and new = consolidated and template ≠ BANK → send (follow-up); if existing has consolidated → `skipped`. Then `analyse` → `format_flash` → `telegram.send` → `save_alert` → `add_event` (payload: `event_version=1, key, isin, symbol, name, exchange, filing_id, pdf_url, disseminated_at, alerted_at, period_end, fy_label, statement, template, confidence, source, rows (crore, 3 cols), metrics, flags, score, verdict`) → optional webhook POST (errors logged only) → schedule commentary task (VERIFIED/LLM_VERIFIED only) that calls `llm_commentary` then `telegram.edit(message_id, append_ai_view(...))`.

- [ ] **Step 1: Write failing tests** with fakes (FakeHttp serving a chosen fixture PDF's bytes for any `.pdf` URL, FakeTelegram recording messages, FakeRouter):
  - `test_verified_flash_and_event` — FakeHttp serves `rossari_page.pdf` → state `done`, 1 message containing `EBITDA`, 1 event with `confidence == "VERIFIED"` and `source == "rules"`.
  - `test_text_layer_typo_goes_vision` — serves `hdfc_page.pdf`; FakeRouter vision returns HDFC truth JSON → event `confidence == "LLM_VERIFIED"`, `rows["pat"][0] == 19509.96` (never the text-layer 19609.96).
  - `test_duplicate_other_exchange_skipped` — same ISIN via BSE after NSE → `skipped`, no download (FakeHttp call count unchanged).
  - `test_seen_skipped` — same filing twice → second `skipped`.
  - `test_broken_pdf_error` — bytes `b"xx"` → `error`, no message (Review Focus 3).
  - `test_too_large_error` — FakeHttp raises `TooLarge` → `error`.
  - `test_image_pdf_vision_path` — Pillow image PDF, FakeRouter vision returns HDFC JSON → `LLM_VERIFIED` message.
  - `test_image_pdf_no_llm_headline` — router `None` → headline with `NO_TEXT`, event confidence `NO_TEXT`.
  - `test_filter_min_mcap` — `filter.enabled`, `min_mcap_cr=200000`, scrip mcap 1000 → `skipped`.
  - `test_llm_timeout_does_not_block` — FakeRouter sleeps 60 s; with timeout patched to 0.1 s → headline sent quickly.
  - `test_telegram_down_event_still_written` — FakeTelegram.send returns `None` → event row exists, alert saved with `message_id None` (Review Focus 5).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: results pipeline`.

### Task 13: App loop, scheduler, health, CLI

**Files:** Create `resultagent/app.py`, `resultagent/__main__.py`; Test `tests/test_app.py`

**Interfaces:**
- Consumes: Pipeline, sources, ScripMaster, Telegram, Store, Settings.
- Produces:
  - `class App(settings)`: `async run()` — refresh scrips if empty or older than 20 h (and daily at 07:00 IST); if `backfill_on_start` is false, fetch today's filings from both sources and `store.mark(..., "seen")` without processing; then start `poll_loop("NSE")`, `poll_loop("BSE")`, `telegram.run()`, `heartbeat_loop()` as tasks; graceful shutdown on SIGTERM (cancel tasks, close Http/Store).
  - `poll_interval(now_ist: datetime, cfg) -> float` (pure; fast 08:00–23:59, slow otherwise, jitter applied by caller).
  - `async poll_once(source, day) -> int` — fetch, `asyncio.create_task(pipeline.process(f))` for unseen filings (bounded by semaphore in pipeline); returns count. Consecutive failures tracked; after 10 min of failures send one Telegram warning `⚠️ {exchange} feed failing for 10 min: {error}` and one recovery note.
  - Around midnight IST also poll yesterday for 10 min (late filings).
  - Heartbeat at 08:30 IST: `✅ Results agent alive · yesterday: {alerts} alerts, {unverified} unread PDFs, {errors} errors`.
  - CLI (`python -m resultagent`): `run [--config PATH]`; `replay --date YYYY-MM-DD [--send]` (processes that day's filings with a no-op Telegram unless `--send`, prints a table `symbol | state | confidence | source | seconds` and the totals — used to measure hit rate); `test-telegram` (sends "Hello from Results Agent"); `get-chat-id`.
  - Logging: stdlib `logging` to stderr, one line per filing: `{exchange} {symbol} {state} {confidence} {latency_s}`.
- [ ] **Step 1: Write failing tests:** `test_poll_interval` (10:00 IST → 10; 02:00 IST → 60; 23:59 → 10); `test_startup_marks_existing_seen` (fake sources return 3 filings; `backfill_on_start=False` → all `seen`, pipeline not called — Review Focus 1); `test_poll_failure_alert_once` (fake source raising for simulated 11 min → exactly one warning message; then success → one recovery message); `test_source_html_does_not_crash_loop` (source raises `HttpError` repeatedly → loop continues, backoff grows, capped at 300 s — Review Focus 2).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run full suite `pytest -q` → all PASS.
- [ ] **Step 5:** Commit `feat: app loop, scheduler, health checks and CLI`.

### Task 14: Deployment, docs, live verification

**Files:** Create `deploy/resultagent.service`, `deploy/install.sh`, `docs/SETUP.md`, `README.md`.

- [ ] **Step 1:** `deploy/resultagent.service`: `User=%i`-free simple unit for user `pi` (placeholder `__USER__`/`__DIR__` replaced by install.sh), `ExecStart=__DIR__/.venv/bin/python -m resultagent run --config __DIR__/config.toml`, `WorkingDirectory=__DIR__`, `Restart=always`, `RestartSec=10`, `MemoryMax=600M`, `Nice=5`, `Environment=PYTHONUNBUFFERED=1`, `NoNewPrivileges=true`, `ProtectSystem=full`.
- [ ] **Step 2:** `deploy/install.sh` (idempotent, `set -euo pipefail`): checks Python ≥ 3.11, `apt-get install -y python3-venv` hint if missing, creates `.venv`, `pip install .`, copies `config.example.toml`→`config.toml` and `.env.example`→`.env` if absent (`chmod 600 .env`), installs the unit to `/etc/systemd/system/resultagent.service` via `sudo`, `systemctl daemon-reload && systemctl enable --now resultagent`; sets `journald` `SystemMaxUse=100M` hint in output.
- [ ] **Step 3:** `docs/SETUP.md` in plain English for a non-developer: flash Raspberry Pi OS Lite 64-bit; copy project (git clone or scp); create Telegram bot with @BotFather; run `python -m resultagent get-chat-id`; get Groq key (console.groq.com) and OpenRouter key (openrouter.ai — recommend one-time $10 credit for 1,000 free requests/day); optional Gemini key; fill `.env`; `./deploy/install.sh`; check with `journalctl -u resultagent -f`; update procedure (`git pull && .venv/bin/pip install . && sudo systemctl restart resultagent`); how to turn on the filter in `config.toml`. `README.md`: one-paragraph overview + link to SETUP.
- [ ] **Step 4: Live verification (from an Indian IP):** run `python -m resultagent replay --date 2026-10-17` and record the table totals in the commit message (counts per confidence; median seconds). Expected: no crashes; ≥ 1 `VERIFIED` (ROSSARI); scanned / OCR-layer PDFs either `LLM_VERIFIED` (with keys) or `NO_TEXT`/`UNVERIFIED`. Spot-check 5 `VERIFIED`/`LLM_VERIFIED` PAT values against the NSE XBRL (`integrated-filing-results`, spec §3) and record matches.
- [ ] **Step 5:** Run `pytest -q` → all PASS.
- [ ] **Step 6:** Commit `chore: pi deployment, setup guide`.
