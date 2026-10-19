"""SQLite memory: saved result facts, notes, chat history, LLM usage, small key/value state."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .redbox import Fact

IST = ZoneInfo("Asia/Kolkata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    symbol TEXT NOT NULL, period TEXT NOT NULL, statement TEXT NOT NULL, metric TEXT NOT NULL,
    basis TEXT NOT NULL, value REAL NOT NULL, prev REAL NOT NULL, is_pct INTEGER NOT NULL,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (symbol, period, statement, metric, basis));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, saved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, text TEXT NOT NULL,
    at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL, tokens INTEGER NOT NULL,
    PRIMARY KEY (day, model));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scrips (code TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL, isin TEXT);
CREATE INDEX IF NOT EXISTS scrips_symbol ON scrips(symbol);
CREATE INDEX IF NOT EXISTS scrips_isin ON scrips(isin);
CREATE TABLE IF NOT EXISTS nse_isin (symbol TEXT PRIMARY KEY, isin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL);
"""


def now_ist() -> datetime:
    return datetime.now(IST).replace(microsecond=0)


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        try:  # per-account chat history (added later)
            self._db.execute("ALTER TABLE chat ADD COLUMN cid TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---- results ----
    def save_facts(self, facts: list[Fact], at: datetime | None = None) -> None:
        """Latest post wins for the same company / period / statement / metric."""
        at = (at or now_ist()).isoformat()
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?)",
                [(f.symbol, f.period, f.statement, f.metric, f.basis, f.value, f.prev, int(f.is_pct), at)
                 for f in facts])

    def facts(self, symbol: str | None = None, since: datetime | None = None) -> list[tuple[Fact, str]]:
        q, args = "SELECT * FROM facts WHERE 1=1", []
        if symbol:
            q, args = q + " AND symbol=?", args + [symbol.upper()]
        if since:
            q, args = q + " AND saved_at>=?", args + [since.isoformat()]
        rows = self._db.execute(q + " ORDER BY saved_at, symbol, metric", args).fetchall()
        return [(Fact(r[0], r[1], r[2], r[3], r[5], r[6], r[4], bool(r[7])), r[8]) for r in rows]

    # ---- notes & chat ----
    def add_note(self, text: str) -> None:
        with self._db:
            self._db.execute("INSERT INTO notes(text, saved_at) VALUES(?,?)", (text, now_ist().isoformat()))

    def notes(self, limit: int = 15) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT text, saved_at FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def add_chat(self, role: str, text: str, cid: str = "") -> None:
        with self._db:
            self._db.execute("INSERT INTO chat(role, text, at, cid) VALUES(?,?,?,?)",
                             (role, text, now_ist().isoformat(), cid))

    def chat(self, limit: int = 10, cid: str = "") -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT role, text FROM chat WHERE cid=? ORDER BY id DESC LIMIT ?",
                                (cid, limit)).fetchall()
        return rows[::-1]

    def clear_chat(self, cid: str = "") -> None:
        with self._db:
            self._db.execute("DELETE FROM chat WHERE cid=?", (cid,))

    # ---- LLM usage (router quotas) ----
    def llm_usage(self, day: date, model: str) -> tuple[int, int]:
        r = self._db.execute("SELECT requests, tokens FROM llm_usage WHERE day=? AND model=?",
                             (day.isoformat(), model)).fetchone()
        return (r[0], r[1]) if r else (0, 0)

    def add_llm_usage(self, day: date, model: str, requests: int, tokens: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO llm_usage VALUES(?,?,?,?) ON CONFLICT(day, model) DO UPDATE SET "
                "requests=requests+excluded.requests, tokens=tokens+excluded.tokens",
                (day.isoformat(), model, requests, tokens))

    # ---- users (access control; status: pending | allowed | denied) ----
    def user(self, uid: str) -> tuple[str, str] | None:
        r = self._db.execute("SELECT name, status FROM users WHERE id=?", (uid,)).fetchone()
        return (r[0], r[1]) if r else None

    def set_user(self, uid: str, status: str, name: str | None = None) -> None:
        old = self.user(uid)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?)",
                             (uid, name or (old[0] if old else uid), status, now_ist().isoformat()))

    def users(self) -> list[tuple[str, str, str]]:
        return self._db.execute("SELECT id, name, status FROM users ORDER BY status, at").fetchall()

    # ---- BSE scrip master ----
    def save_scrips(self, rows: list[tuple]) -> None:
        with self._db:
            self._db.executemany("INSERT OR REPLACE INTO scrips VALUES(?,?,?,?)", rows)

    def scrip_count(self) -> int:
        return self._db.execute("SELECT count(*) FROM scrips").fetchone()[0]

    def save_nse_isins(self, rows: list[tuple[str, str]]) -> None:
        with self._db:
            self._db.executemany("INSERT OR REPLACE INTO nse_isin VALUES(?,?)", rows)

    def scrip_code(self, symbol: str) -> str | None:
        """BSE code for an NSE symbol: exact ISIN match (most reliable), same id, then company name."""
        s = symbol.upper()
        r = self._db.execute("SELECT s.code FROM nse_isin n JOIN scrips s ON s.isin=n.isin WHERE n.symbol=?",
                             (s,)).fetchone() or \
            self._db.execute("SELECT code FROM scrips WHERE symbol=?", (s,)).fetchone() or \
            self._db.execute("SELECT code FROM scrips WHERE upper(name) LIKE ? ORDER BY length(name) LIMIT 1",
                             (symbol.upper() + "%",)).fetchone()
        return r[0] if r else None

    def known_symbols(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT symbol FROM scrips UNION SELECT symbol FROM nse_isin")}

    # ---- key/value ----
    def get(self, k: str, default: str | None = None) -> str | None:
        r = self._db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, k: str, v: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
