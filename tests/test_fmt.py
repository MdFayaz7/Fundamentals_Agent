from datetime import datetime, timezone

from resultagent.analyse import analyse
from resultagent.fmt import append_ai_view, fmt_cr, fmt_pct, format_flash, format_headline
from resultagent.models import Confidence, Filing

DISS = datetime(2026, 10, 18, 9, 30, 0, tzinfo=timezone.utc)
ALERT = datetime(2026, 10, 18, 9, 30, 12, tzinfo=timezone.utc)


def _filing(name="HDFC Bank Ltd"):
    return Filing("NSE", "1", "INE040A01034", "HDFCBANK", name,
                  "https://example.com/r.pdf", DISS, "Financial Results")


def test_fmt_cr():
    assert fmt_cr(112400) == "₹1,12,400 Cr"
    assert fmt_cr(34619.18) == "₹34,619 Cr"
    assert fmt_cr(86.974) == "₹87.0 Cr"
    assert fmt_cr(3.67) == "₹3.67 Cr"
    assert fmt_cr(-12.5) == "−₹12.5 Cr"
    assert fmt_cr(None) == "n/a"


def test_fmt_pct():
    assert fmt_pct(9.72) == "+9.7%"
    assert fmt_pct(-3.24) == "−3.2%"
    assert fmt_pct(20, "bps") == "+20 bps"
    assert fmt_pct(-0.13, "pp") == "−0.13 pp"
    assert fmt_pct(None) == "n/a"


def test_flash_contains(hdfc_truth):
    a = analyse(hdfc_truth)
    out = format_flash(_filing(), hdfc_truth, a, Confidence.VERIFIED, ALERT)
    for s in ("<b>HDFCBANK</b>", "Q2 FY27 (Standalone)", "NII", "₹34,619 Cr",
              "What this means:", 'href="https://', "✔ verified", "(12 s)", "15:00:12"):
        assert s in out
    assert len(out) <= 1500


def test_escapes_html(hdfc_truth):
    a = analyse(hdfc_truth)
    out = format_flash(_filing("A&B <Ltd>"), hdfc_truth, a, Confidence.LLM_VERIFIED, ALERT)
    assert "A&amp;B &lt;Ltd&gt;" in out
    assert "AI-read, verified" in out


def test_headline():
    out = format_headline(_filing(), "Could not read PDF", ALERT)
    assert "⚪ <b>HDFCBANK</b> · HDFC Bank Ltd — results filed" in out
    assert "Could not read PDF" in out
    assert 'href="https://example.com/r.pdf"' in out


def test_append_ai_view():
    out = append_ai_view("base", ["a < b", "c"])
    assert out.startswith("base") and "a &lt; b" in out


def test_ai_view_capped():
    out = append_ai_view("x" * 1490, ["y" * 300] * 10)
    assert len(out) <= 4096
    assert out.count("•") <= 4


def test_ai_view_escape_not_split():
    out = append_ai_view("base", ["&<>" * 100])
    bullet = out.split("• ", 1)[1]
    assert bullet.endswith("…")
    body = bullet[:-1]
    assert body == "&amp;&lt;&gt;" * (len(body) // 13) and len(body) % 13 == 0


def test_flash_long_flags_and_text(hdfc_truth):
    from resultagent.models import Flag

    a = analyse(hdfc_truth)
    a.flags = [Flag(True, "f" * 200) for _ in range(20)]
    assert len(format_flash(_filing(), hdfc_truth, a, Confidence.VERIFIED, ALERT)) <= 1500
    a.summary = "s" * 3000
    out = format_flash(_filing("N" * 2000), hdfc_truth, a, Confidence.VERIFIED, ALERT)
    assert len(out) <= 1500 and "…" in out


def test_negative_latency(hdfc_truth):
    a = analyse(hdfc_truth)
    out = format_flash(_filing(), hdfc_truth, a, Confidence.VERIFIED, DISS.replace(second=0))
    assert "(0 s)" in out
