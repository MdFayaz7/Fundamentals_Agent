from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from resultagent.models import Analysis, Confidence, Extraction, Filing, Metric
from resultagent.periods import fy_label

IST = ZoneInfo("Asia/Kolkata")
MINUS = "−"
MAX_LEN = 1500
_TEMPLATE_LABEL = {"corporate": "Company", "bank": "Bank", "lender": "NBFC"}
_EMOJI = {"STRONG": "🟢", "GOOD": "🟢", "MIXED": "🟡", "WEAK": "🔴", "POOR": "🔴"}


def _indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def fmt_cr(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1000:
        body = _indian(round(a))
    elif a >= 10:
        body = f"{a:.1f}"
    else:
        body = f"{a:.2f}"
    return f"{MINUS if v < 0 else ''}₹{body} Cr"


def fmt_pct(v: float | None, kind: str = "pct") -> str:
    if v is None:
        return "n/a"
    sign = MINUS if v < 0 else "+"
    a = abs(v)
    if kind == "bps":
        return f"{sign}{a:.0f} bps"
    if kind == "pp":
        return f"{sign}{a:.2f} pp"
    return f"{sign}{a:.1f}%"


def _value(m: Metric) -> str:
    if m.value is None:
        return "n/a"
    if m.kind == "eps":
        return f"₹{m.value:.2f}"
    if m.kind == "pct":
        return f"{m.value:.2f}%"
    if m.kind == "margin":
        return f"{m.value:.1f}%"
    return fmt_cr(m.value)


def _delta(m: Metric, v: float | None) -> str:
    kind = {"margin": "bps", "pct": "pp"}.get(m.kind, "pct")
    return fmt_pct(v, kind)


def _metric_line(m: Metric) -> str:
    return (f"{m.name:<11} {_value(m):>15}  YoY {_delta(m, m.yoy):>7}"
            f"  QoQ {_delta(m, m.qoq):>7}")


def _ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%H:%M:%S")


def _footer(f: Filing, alerted_at: datetime) -> str:
    n = max(0, round((alerted_at - f.disseminated_at).total_seconds()))
    label = "Filing" if f.subject == "XBRL" else "PDF"
    return (f'📄 <a href="{escape(f.pdf_url, quote=True)}">{label}</a> · {escape(f.exchange)} '
            f"{_ist(f.disseminated_at)} · alert {_ist(alerted_at)} ({n} s)")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _render(f, ex, a, conf, alerted_at, flags, name=None, summary=None) -> str:
    name = f.name if name is None else name
    summary = a.summary if summary is None else summary
    emoji = _EMOJI.get(a.verdict.upper(), "⚪")
    period = fy_label(ex.period_end) if ex.period_end else "Results"
    label = _TEMPLATE_LABEL.get(str(ex.template), str(ex.template))
    badge = "✔ exact (exchange XBRL)" if ex.source == "xbrl" else "✔ verified"
    lines = [
        f"{emoji} <b>{escape(f.symbol)}</b> · {escape(name)} · {period} "
        f"({ex.statement.capitalize()}) · {label}",
        f"Verdict: <b>{escape(a.verdict)}</b> · Score {a.score}/10 · {badge}",
        "<pre>" + escape("\n".join(_metric_line(m) for m in a.metrics)) + "</pre>",
    ]
    lines += [f"{'✅' if fl.positive else '⚠️'} {escape(fl.text)}" for fl in flags]
    lines.append(f"<i>What this means:</i> {escape(summary)}")
    lines.append(_footer(f, alerted_at))
    return "\n".join(lines)


def format_flash(f: Filing, ex: Extraction, a: Analysis, conf: Confidence,
                 alerted_at: datetime) -> str:
    flags = list(a.flags)
    out = _render(f, ex, a, conf, alerted_at, flags)
    while len(out) > MAX_LEN and flags:
        flags.pop()
        out = _render(f, ex, a, conf, alerted_at, flags)
    summary, name = a.summary, f.name
    while len(out) > MAX_LEN and (len(summary) > 1 or len(name) > 1):
        if len(summary) > 1:
            summary = _cut(summary, max(1, len(summary) - max(1, len(out) - MAX_LEN)))
        else:
            name = _cut(name, max(1, len(name) - max(1, len(out) - MAX_LEN)))
        out = _render(f, ex, a, conf, alerted_at, flags, name, summary)
    return out


def format_instant(f: Filing, alerted_at: datetime) -> str:
    """First message, sent the moment a result filing appears (edited later with the numbers)."""
    return "\n".join([
        f"⚡ <b>{escape(f.symbol or f.name)}</b> · {escape(f.name)} — results out",
        "Full report follows here when the company files its XBRL data (usually 1–4 h).",
        _footer(f, alerted_at),
    ])


def format_headline(f: Filing, reason: str, alerted_at: datetime) -> str:
    return "\n".join([
        f"⚪ <b>{escape(f.symbol)}</b> · {escape(f.name)} — results filed",
        escape(reason),
        _footer(f, alerted_at),
    ])


def append_ai_view(html: str, bullets: list[str]) -> str:
    items = [f"• {escape(_cut(b, 160))}" for b in bullets[:4]]
    while items:
        out = html + "\n\n<b>AI view</b>\n" + "\n".join(items)
        if len(out) <= 4096:
            return out
        items.pop()
    return html
