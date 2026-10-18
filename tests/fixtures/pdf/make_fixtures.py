"""Dev script: extract single pages from real result PDFs into test fixtures.

Usage: python make_fixtures.py HDFCBANK.pdf ROSSARI.pdf
Source PDFs are untrusted data; only opened with pypdfium2.
Image objects (scan backgrounds) are stripped to keep fixtures small; the
text layer is what the tests need.
"""
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

OUT = Path(__file__).parent


def extract(src: str, index: int, dest: Path) -> None:
    doc = pdfium.PdfDocument(src)
    stage = pdfium.PdfDocument.new()
    stage.import_pages(doc, [index])
    page = stage[0]
    for obj in [o for o in page.get_objects() if o.type == pdfium_c.FPDF_PAGEOBJ_IMAGE]:
        page.remove_obj(obj)
    page.gen_content()
    page.close()
    final = pdfium.PdfDocument.new()
    final.import_pages(stage, [0])  # re-import drops orphaned image streams
    final.save(str(dest))
    final.close()
    stage.close()
    doc.close()


if __name__ == "__main__":
    extract(sys.argv[1], 1, OUT / "hdfc_page.pdf")
    extract(sys.argv[2], 5, OUT / "rossari_page.pdf")
