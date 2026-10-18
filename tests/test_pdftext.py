import io
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from resultagent.models import Template
from resultagent.pdftext import (
    close,
    find_result_pages,
    load_pdf,
    render_jpeg,
    vision_candidates,
)

FIX = Path(__file__).parent / "fixtures" / "pdf"


def _image_only_pdf() -> bytes:
    pages = []
    for i in range(3):
        img = Image.new("L", (1240, 1754), 255)
        ImageDraw.Draw(img).text((100, 100), f"scan {i}", fill=0)
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def test_load_and_find_hdfc():
    info = load_pdf((FIX / "hdfc_page.pdf").read_bytes())
    try:
        assert find_result_pages(info) == [(0, Template.BANK, "standalone")]
    finally:
        close(info)


def test_find_rossari():
    info = load_pdf((FIX / "rossari_page.pdf").read_bytes())
    try:
        assert find_result_pages(info) == [(0, Template.CORPORATE, "consolidated")]
    finally:
        close(info)


def test_bad_pdf_raises():
    with pytest.raises(ValueError):
        load_pdf(b"not a pdf")


def test_image_only_pdf_vision_candidates():
    info = load_pdf(_image_only_pdf())
    try:
        assert vision_candidates(info) == [0, 1]
        assert find_result_pages(info) == []
    finally:
        close(info)


def test_render_jpeg():
    info = load_pdf((FIX / "hdfc_page.pdf").read_bytes())
    try:
        data = render_jpeg(info, 0)
    finally:
        close(info)
    assert data.startswith(b"\xff\xd8")
    assert len(data) < 600_000
    assert max(Image.open(io.BytesIO(data)).size) <= 2000
