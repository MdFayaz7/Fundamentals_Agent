"""SQLite persistence: filing dedupe, alerts, events, LLM usage, scrip master."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_SCRIP_COLS = ("code", "isin", "symbol", "name", "mcap_cr", "grp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    exchange TEXT NOT NULL, filing_id TEXT NOT NULL, key TEXT, state TEXT NOT NULL,
    updated_ist_date TEXT NOT NULL,
    PRIMARY KEY (exchange, filing_id));
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT NOT NULL, period_end TEXT NOT NULL, statement TEXT NOT NULL,
    exchange TEXT, filing_id TEXT, message_id INTEGER, ist_date TEXT NOT NULL,
    PRIMARY KEY (key, period_end, statement));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    key TEXT, symbol TEXT, period_end TEXT, confidence REAL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL, tokens INTEGER NOT NULL,
    PRIMARY KEY (day, model));
CREATE TABLE IF NOT EXISTS scrips (
    code TEXT PRIMARY KEY, isin TEXT, symbol TEXT, name TEXT, mcap_cr REAL, grp TEXT);
CREATE INDEX IF NOT EXISTS scrips_isin ON scrips(isin);
"""


def _ist_today() -> date:
    return datetime.now(_IST).date()


def _iso(v) -> str | None:
    return None if v is None else (v.isoformat() if isinstance(v, date) else str(v))


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def seen(self, exchange: str, filing_id: str) -> bool:
        r = self._db.execute(
            "SELECT 1 FROM filings WHERE exchange=? AND filing_id=?", (exchange, filing_id)
        ).fetchone()
        return r is not None

    def mark(self, exchange: str, filing_id: str, key: str, state: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO filings(exchange, filing_id, key, state, updated_ist_date) "
                "VALUES(?,?,?,?,?) ON CONFLICT(exchange, filing_id) DO UPDATE SET "
                "key=excluded.key, state=excluded.state, updated_ist_date=excluded.updated_ist_date",
                (exchange, filing_id, key, state, _ist_today().isoformat()),
            )

    def alert_today_other_exchange(self, key: str, exchange: str, ist_date: date) -> bool:
        r = self._db.execute(
            "SELECT 1 FROM alerts WHERE key=? AND ist_date=? AND exchange<>? LIMIT 1",
            (key, ist_date.isoformat(), exchange),
        ).fetchone()
        return r is not None

    def alert_exists(self, key: str, period_end: date, statement: str) -> bool:
        r = self._db.execute(
            "SELECT 1 FROM alerts WHERE key=? AND period_end=? AND statement=?",
            (key, period_end.isoformat(), statement),
        ).fetchone()
        return r is not None

    def alert_statements(self, key: str, period_end: date) -> set[str]:
        rows = self._db.execute(
            "SELECT statement FROM alerts WHERE key=? AND period_end=?",
            (key, period_end.isoformat()),
        ).fetchall()
        return {r[0] for r in rows}

    def save_alert(self, key: str, period_end: date, statement: str, exchange: str,
                   filing_id: str, message_id: int | None, ist_date: date) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO alerts(key, period_end, statement, exchange, filing_id, "
                "message_id, ist_date) VALUES(?,?,?,?,?,?,?)",
                (key, period_end.isoformat(), statement, exchange, filing_id, message_id,
                 ist_date.isoformat()),
            )

    def add_event(self, payload: dict) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO events(created_at, key, symbol, period_end, confidence, json) "
                "VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), payload.get("key"), payload.get("symbol"),
                 _iso(payload.get("period_end")), payload.get("confidence"),
                 json.dumps(payload, default=str)),
            )
        return int(cur.lastrowid)

    def llm_usage(self, day: date, model: str) -> tuple[int, int]:
        r = self._db.execute(
            "SELECT requests, tokens FROM llm_usage WHERE day=? AND model=?",
            (day.isoformat(), model),
        ).fetchone()
        return (r[0], r[1]) if r else (0, 0)

    def add_llm_usage(self, day: date, model: str, requests: int, tokens: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO llm_usage(day, model, requests, tokens) VALUES(?,?,?,?) "
                "ON CONFLICT(day, model) DO UPDATE SET requests=requests+excluded.requests, "
                "tokens=tokens+excluded.tokens",
                (day.isoformat(), model, requests, tokens),
            )

    def upsert_scrips(self, rows: list[dict]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO scrips(code, isin, symbol, name, mcap_cr, grp) "
                "VALUES(?,?,?,?,?,?)",
                [tuple(r.get(c) for c in _SCRIP_COLS) for r in rows],
            )

    def _scrip(self, col: str, val: str) -> dict | None:
        r = self._db.execute(
            f"SELECT {', '.join(_SCRIP_COLS)} FROM scrips WHERE {col}=? LIMIT 1", (val,)
        ).fetchone()
        return dict(zip(_SCRIP_COLS, r)) if r else None

    def scrip_by_code(self, code: str) -> dict | None:
        return self._scrip("code", code)

    def scrip_by_isin(self, isin: str) -> dict | None:
        r = self._scrip("isin", isin)
        if r is None and len(isin) >= 9:  # same issuer + equity, different ISIN serial
            row = self._db.execute(
                f"SELECT {', '.join(_SCRIP_COLS)} FROM scrips WHERE isin LIKE ? LIMIT 1", (isin[:9] + "%",)
            ).fetchone()
            r = dict(zip(_SCRIP_COLS, row)) if row else None
        return r

    def stats_for_day(self, ist_date: date) -> dict:
        d = ist_date.isoformat()
        one = lambda sql, *a: self._db.execute(sql, a).fetchone()[0]
        return {
            "alerts": one("SELECT COUNT(*) FROM alerts WHERE ist_date=?", d),
            "unverified": one("SELECT COUNT(*) FROM filings WHERE state='unverified' AND updated_ist_date=?", d),
            "errors": one("SELECT COUNT(*) FROM filings WHERE state='error' AND updated_ist_date=?", d),
        }
