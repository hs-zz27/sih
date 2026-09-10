"""H1 - multimodal ingestion pipeline.

Accepts a PDF or an image file and returns page-level `Document`s:

* Born-digital PDF pages use PyMuPDF's text layer directly.
* Pages with no usable text layer (below `ingestion.text_layer_min_chars`),
  and any plain image upload, are rasterised/opened and run through Tesseract.

Runs entirely offline - PyMuPDF and pytesseract are both local libraries, and
Tesseract itself is a local binary. There is nothing in this module capable of
making a network call.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image

from src import config
from src.contracts import Document, IngestResult, new_id, utcnow

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
PDF_SUFFIXES = {".pdf"}


def _ocr_page(image: Image.Image) -> tuple[str, float]:
    """Run Tesseract on one page image. Returns (text, mean confidence 0-1)."""
    lang = config.get("ingestion.ocr_lang", "eng")
    data = pytesseract.image_to_data(
        image, lang=lang, output_type=pytesseract.Output.DICT
    )
    words = [w for w in data["text"] if w.strip()]
    confidences = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    text = " ".join(words)
    mean_confidence = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
    return text, mean_confidence


def _ingest_pdf(path: Path) -> tuple[list[Document], int]:
    min_chars = config.get("ingestion.text_layer_min_chars", 40)
    dpi = config.get("ingestion.ocr_dpi", 300)

    documents: list[Document] = []
    ocr_pages = 0

    with pymupdf.open(path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            text_layer = page.get_text().strip()

            if len(text_layer) >= min_chars:
                documents.append(
                    Document(
                        source_path=str(path),
                        page=page_index,
                        text=text_layer,
                        metadata={
                            "extraction_method": "text_layer",
                            "filename": path.name,
                            "mime_type": "application/pdf",
                            "page_count": pdf.page_count,
                            "ingested_at": utcnow().isoformat(),
                        },
                    )
                )
                continue

            pix = page.get_pixmap(dpi=dpi)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text, confidence = _ocr_page(image)
            ocr_pages += 1

            documents.append(
                Document(
                    source_path=str(path),
                    page=page_index,
                    text=ocr_text,
                    metadata={
                        "extraction_method": "ocr",
                        "ocr_confidence": confidence,
                        "filename": path.name,
                        "mime_type": "application/pdf",
                        "page_count": pdf.page_count,
                        "ingested_at": utcnow().isoformat(),
                    },
                )
            )

    return documents, ocr_pages


def _ingest_image(path: Path) -> tuple[list[Document], int]:
    image = Image.open(path)
    text, confidence = _ocr_page(image)

    document = Document(
        source_path=str(path),
        page=1,
        text=text,
        metadata={
            "extraction_method": "ocr",
            "ocr_confidence": confidence,
            "filename": path.name,
            "mime_type": f"image/{path.suffix.lstrip('.').lower()}",
            "page_count": 1,
            "ingested_at": utcnow().isoformat(),
        },
    )
    return [document], 1


def ingest_file(path: Path) -> IngestResult:
    """Extract page-level `Document`s from a local PDF or image file."""
    path = Path(path)
    suffix = path.suffix.lower()
    warnings: list[str] = []

    if suffix in PDF_SUFFIXES:
        documents, ocr_pages = _ingest_pdf(path)
    elif suffix in IMAGE_SUFFIXES:
        documents, ocr_pages = _ingest_image(path)
    else:
        raise ValueError(f"Unsupported file type: {suffix!r} ({path.name})")

    for document in documents:
        confidence = document.metadata.get("ocr_confidence")
        if confidence is not None and confidence < 0.5:
            warnings.append(
                f"page {document.page}: low OCR confidence ({confidence:.0%}) - verify manually"
            )
        if document.metadata["extraction_method"] == "ocr" and not document.text.strip():
            warnings.append(f"page {document.page}: OCR returned no text")

    return IngestResult(
        source_path=str(path),
        filename=path.name,
        page_count=len(documents),
        ocr_pages=ocr_pages,
        documents=documents,
        warnings=warnings,
    )
