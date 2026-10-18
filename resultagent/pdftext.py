from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pypdfium2 as pdfium

from resultagent.extract import detect_statement, detect_template
from resultagent.models import Template
from resultagent.numparse import tokenize_numbers

MAX_TEXT_PAGES = 30
_LABEL = re.compile(
    r"revenue\s*from\s*operations|interest\s*earned|total\s*income|interest\s*income",
    re.IGNORECASE,
)


@dataclass
class PdfInfo:
    pages: list[str]
    doc: pdfium.PdfDocument


def load_pdf(data: bytes) -> PdfInfo:
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as e:
        raise ValueError("bad pdf") from e
    try:
        n = len(doc)
        if n == 0:
            raise ValueError("bad pdf")
        pages: list[str] = []
        for i in range(min(n, MAX_TEXT_PAGES)):
            page = doc[i]
            try:
                tp = page.get_textpage()
                try:
                    pages.append(tp.get_text_range())
                finally:
                    tp.close()
            finally:
                page.close()
    except Exception as e:
        doc.close()
        raise ValueError("bad pdf") from e
    return PdfInfo(pages=pages, doc=doc)


def _has_label_numbers(text: str) -> bool:
    for line in text.splitlines():
        m = _LABEL.search(line)
        if m and len(tokenize_numbers(line[m.end():])) >= 3:
            return True
    return False


def find_result_pages(info: PdfInfo) -> list[tuple[int, Template, str]]:
    out = []
    for i, text in enumerate(info.pages):
        tpl = detect_template(text)
        if tpl is not None and _has_label_numbers(text):
            out.append((i, tpl, detect_statement(text)))
    return out


def pick_page(
    cands: list[tuple[int, Template, str]], prefer_bank_standalone: bool = True
) -> tuple[int, Template, str] | None:
    if not cands:
        return None
    for c in cands:
        if c[1] == Template.BANK:
            if c[2] == ("standalone" if prefer_bank_standalone else "consolidated"):
                return c
        elif c[2] == "consolidated":
            return c
    return cands[0]


def vision_candidates(info: PdfInfo, limit: int = 2) -> list[int]:
    out = []
    for i, text in enumerate(info.pages[:8]):
        if len("".join(text.split())) < 200:
            out.append(i)
            if len(out) >= limit:
                break
    return out


def render_jpeg(info: PdfInfo, index: int, dpi: int = 150) -> bytes:
    page = info.doc[index]
    try:
        w, h = page.get_size()
        scale = dpi / 72
        longest = max(w, h) * scale
        if longest > 2000:
            scale *= 2000 / longest
        bitmap = page.render(scale=scale, grayscale=True)
        try:
            img = bitmap.to_pil().convert("L")
        finally:
            bitmap.close()
    finally:
        page.close()
    if max(img.size) > 2000:
        img.thumbnail((2000, 2000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    img.close()
    return buf.getvalue()


def close(info: PdfInfo) -> None:
    try:
        info.doc.close()
    except Exception:
        pass
