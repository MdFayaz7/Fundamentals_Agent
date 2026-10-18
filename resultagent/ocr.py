"""Local OCR with Tesseract (apt install tesseract-ocr). Runs as a subprocess so its memory is
freed after every page — important on a 2 GB Pi. No tokens, no network."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil

from .pdftext import PdfInfo

log = logging.getLogger(__name__)
_one_at_a_time = asyncio.Semaphore(1)


def available() -> bool:
    return shutil.which("tesseract") is not None


def _png(info: PdfInfo, index: int, dpi: int = 300, max_side: int = 3600) -> bytes:
    page = info.doc[index]
    try:
        w, h = page.get_size()
        scale = min(dpi / 72, max_side / max(w, h))
        bitmap = page.render(scale=scale, grayscale=True)
        try:
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            img.close()
            return buf.getvalue()
        finally:
            bitmap.close()
    finally:
        page.close()


async def ocr_page(info: PdfInfo, index: int, timeout: float = 40.0) -> str | None:
    """Text of one page with table rows kept on one line each, or None on failure."""
    async with _one_at_a_time:
        png = _png(info, index)
        proc = await asyncio.create_subprocess_exec(
            "tesseract", "stdin", "stdout", "--psm", "6", "-c", "preserve_interword_spaces=1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(png), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("tesseract timed out on page %d", index)
            return None
        return out.decode("utf-8", "ignore") if proc.returncode == 0 else None
