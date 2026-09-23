from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument
from pypdf import PdfReader


class DocumentParsingError(Exception):
    pass


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return _extract_txt(file_path)
    if suffix == ".pdf":
        return _extract_pdf(file_path)
    if suffix == ".docx":
        return _extract_docx(file_path)
    raise DocumentParsingError(f"Unsupported file extension: {suffix}")


def _extract_txt(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8", errors="ignore").strip()


def _extract_pdf(file_path: Path) -> str:
    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        raise DocumentParsingError(f"Could not read PDF: {exc}") from exc
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_docx(file_path: Path) -> str:
    try:
        doc = DocxDocument(str(file_path))
    except Exception as exc:
        raise DocumentParsingError(f"Could not read DOCX: {exc}") from exc
    return "\n".join(p.text for p in doc.paragraphs).strip()
