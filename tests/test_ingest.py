"""H1 - ingestion pipeline tests.

Exercises both paths: a born-digital PDF (text layer, no OCR) and a scanned
image (OCR required) - using the actual degraded corpus images checked into
data/corpus/, so this is testing against real scan quality, not a mock.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from src.io.ingest import ingest_file

# OCR needs the tesseract *binary*, not just pytesseract. Skip cleanly where it
# is not installed so the suite stays green on every teammate's machine.
def _tesseract_available() -> bool:
    import shutil

    if shutil.which("tesseract"):
        return True
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


_NEEDS_OCR = pytest.mark.skipif(
    not _tesseract_available(), reason="tesseract binary not installed on this machine"
)

CORPUS = Path(__file__).parent.parent / "data" / "corpus"


def test_born_digital_pdf_uses_text_layer(tmp_path):
    pdf_path = tmp_path / "born_digital.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "SOP-114 - Heat Exchanger Inspection Procedure")
    page.insert_text((72, 100), "Retirement threshold for CDU overhead service: 7.6 mm.")
    doc.save(pdf_path)
    doc.close()

    result = ingest_file(pdf_path)

    assert result.page_count == 1
    assert result.ocr_pages == 0
    assert result.documents[0].metadata["extraction_method"] == "text_layer"
    assert "7.6 mm" in result.documents[0].text


@pytest.mark.parametrize(
    "filename",
    [
        "INSP-2026-0412_E-4102_scan.jpg",
        "SOP-114_heat_exchanger_inspection_scan.jpg",
    ],
)
@_NEEDS_OCR
def test_degraded_scan_falls_back_to_ocr(filename):
    path = CORPUS / filename
    if not path.exists():
        pytest.skip(f"{filename} not present in data/corpus/")

    result = ingest_file(path)

    assert result.page_count == 1
    assert result.ocr_pages == 1
    document = result.documents[0]
    assert document.metadata["extraction_method"] == "ocr"
    assert "ocr_confidence" in document.metadata
    assert "MANGALA" in document.text.upper()


def test_unsupported_extension_raises(tmp_path):
    bogus = tmp_path / "not_a_document.exe"
    bogus.write_bytes(b"nope")
    with pytest.raises(ValueError):
        ingest_file(bogus)
