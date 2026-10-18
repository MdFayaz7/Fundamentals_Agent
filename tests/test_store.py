from datetime import date

from resultagent.store import Store

D = date(2026, 10, 18)


def test_seen_mark_upsert(tmp_path):
    s = Store(tmp_path / "sub" / "a.db")
    assert not s.seen("NSE", "f1")
    s.mark("NSE", "f1", "K", "seen")
    assert s.seen("NSE", "f1")
    s.mark("NSE", "f1", "K", "done")
    assert s.seen("NSE", "f1")
    assert not s.seen("BSE", "f1")


def test_alert_today_other_exchange(tmp_path):
    s = Store(tmp_path / "a.db")
    s.save_alert("K", D, "standalone", "NSE", "f1", 5, D)
    assert s.alert_today_other_exchange("K", "BSE", D)
    assert not s.alert_today_other_exchange("K", "NSE", D)
    assert not s.alert_today_other_exchange("K", "BSE", date(2026, 10, 19))


def test_alerts_roundtrip_and_duplicate(tmp_path):
    s = Store(tmp_path / "a.db")
    assert not s.alert_exists("K", D, "standalone")
    s.save_alert("K", D, "standalone", "NSE", "f1", None, D)
    s.save_alert("K", D, "standalone", "BSE", "f2", 1, D)
    s.save_alert("K", D, "consolidated", "NSE", "f1", 2, D)
    assert s.alert_exists("K", D, "standalone")
    assert s.alert_statements("K", D) == {"standalone", "consolidated"}


def test_add_event(tmp_path):
    s = Store(tmp_path / "a.db")
    i = s.add_event({"key": "K", "symbol": "ABC", "period_end": D, "confidence": 0.9})
    j = s.add_event({"x": 1})
    assert j == i + 1
    row = s._db.execute("select key,symbol,period_end,confidence,json,created_at from events where id=?", (i,)).fetchone()
    assert row[:4] == ("K", "ABC", "2026-10-18", 0.9)
    assert "2026-10-18" in row[4] and row[5]
    row = s._db.execute("select key,symbol from events where id=?", (j,)).fetchone()
    assert row == (None, None)


def test_llm_usage(tmp_path):
    s = Store(tmp_path / "a.db")
    assert s.llm_usage(D, "m") == (0, 0)
    s.add_llm_usage(D, "m", 1, 100)
    s.add_llm_usage(D, "m", 2, 50)
    assert s.llm_usage(D, "m") == (3, 150)


def test_scrips(tmp_path):
    s = Store(tmp_path / "a.db")
    rows = [dict(code=str(i), isin=f"INE{i}", symbol=f"S{i}", name=f"N{i}", mcap_cr=1.5, grp="A") for i in range(5000)]
    s.upsert_scrips(rows)
    assert s.scrip_by_code("42") == rows[42]
    assert s.scrip_by_isin("INE43") == rows[43]
    assert s.scrip_by_code("nope") is None
    assert s.scrip_by_isin("nope") is None
    s.upsert_scrips([dict(rows[42], name="new")])
    assert s.scrip_by_code("42")["name"] == "new"


def test_stats_for_day(tmp_path):
    s = Store(tmp_path / "a.db")
    from resultagent.store import _ist_today
    today = _ist_today()
    s.save_alert("K", D, "standalone", "NSE", "f1", 1, today)
    s.mark("NSE", "a", "K", "unverified")
    s.mark("NSE", "b", "K", "error")
    s.mark("NSE", "c", "K", "done")
    assert s.stats_for_day(today) == {"alerts": 1, "unverified": 1, "errors": 1}
    assert s.stats_for_day(date(2000, 1, 1)) == {"alerts": 0, "unverified": 0, "errors": 0}


def test_reopen_persists(tmp_path):
    p = tmp_path / "a.db"
    s = Store(p)
    s.mark("NSE", "f1", "K", "done")
    s.close()
    assert Store(p).seen("NSE", "f1")
