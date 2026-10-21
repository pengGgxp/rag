import csv
import io
from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import load_workbook
from pypdf import PdfReader

from .parsed_document import ParsedDocument, assemble_pages


class UnsupportedDocumentError(ValueError):
    pass


def parse_document(filename: str, content_type: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if content_type in {"text/plain", "text/markdown"} or suffix in {".txt", ".md"}:
        return content.decode("utf-8", errors="replace"), "plain-text"
    if content_type == "text/html" or suffix in {".html", ".htm"}:
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []

            def handle_data(self, data: str) -> None:
                if data.strip():
                    self.parts.append(data.strip())

        extractor = TextExtractor()
        extractor.feed(content.decode("utf-8", errors="replace"))
        return "\n".join(extractor.parts), "html"
    if content_type == "text/csv" or suffix == ".csv":
        rows = csv.reader(io.StringIO(content.decode("utf-8", errors="replace")))
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows), "csv"
    if suffix == ".xlsx":
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                lines.append(" | ".join("" if value is None else str(value) for value in row))
        return "\n".join(lines), "xlsx"
    if suffix == ".pdf" or content_type == "application/pdf":
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages), "pypdf"
    if suffix == ".docx":
        document = DocxDocument(io.BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs), "python-docx"
    raise UnsupportedDocumentError(f"No parser is configured for {filename}")


def pdf_pages(content: bytes):
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF")
        if not reader.pages:
            raise ValueError("Empty PDF")
        return reader.pages
    except Exception:
        raise ValueError("无法读取 PDF，请检查文件是否损坏或受密码保护") from None


def parse_local_pdf(content: bytes) -> ParsedDocument:
    pages = [(index, [{"text": page.extract_text() or "", "block_type": "text"}])
             for index, page in enumerate(pdf_pages(content), start=1)]
    return assemble_pages(pages, "pypdf")
