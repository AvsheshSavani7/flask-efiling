import io
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent


def _ocr_document(document, dpi: int = 300, lang: str = "eng") -> str:
    """OCR every page of an open PyMuPDF document and return the joined text."""
    extracted_pages = []
    try:
        for page_number, page in enumerate(document, start=1):
            # Higher DPI generally improves OCR accuracy.
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            text = pytesseract.image_to_string(
                image,
                lang=lang,
                config="--psm 6",
            )
            extracted_pages.append(f"\n--- Page {page_number} ---\n{text.strip()}")
    finally:
        document.close()
    return "\n".join(extracted_pages)


def extract_text_from_scanned_pdf(pdf_path: str, dpi: int = 300, lang: str = "eng") -> str:
    """OCR a scanned PDF from a file path."""
    return _ocr_document(fitz.open(pdf_path), dpi=dpi, lang=lang)


def extract_text_from_scanned_pdf_bytes(
    pdf_bytes: bytes, dpi: int = 300, lang: str = "eng"
) -> str:
    """OCR a scanned PDF from in-memory bytes (used by the docket scrapers)."""
    if not pdf_bytes:
        return ""
    return _ocr_document(
        fitz.open(stream=pdf_bytes, filetype="pdf"), dpi=dpi, lang=lang
    )


if __name__ == "__main__":
    pdf_file = SCRIPT_DIR / "ViewImage.pdf"
    text = extract_text_from_scanned_pdf(str(pdf_file))

    output_file = SCRIPT_DIR / "extracted_text.txt"
    with open(output_file, "w", encoding="utf-8") as file:
        file.write(text)

    print(text)
