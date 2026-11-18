import csv
import io
from pathlib import Path

from docx import Document as DocxDocument
from openpyxl import load_workbook
from pypdf import PdfReader
from pypdf.errors import EmptyFileError, PdfReadError

from .parsed_document import ParsedDocument, assemble_pages


class UnsupportedDocumentError(ValueError):
    pass


class PdfParsingError(ValueError):
    """A user-actionable PDF failure, shared by ingestion and attachments."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message)
        self.code = code

    def __str__(self) -> str:
        return self.args[1]


PDF_INVALID_MESSAGE = "PDF 文件已损坏或格式无效，请重新导出后上传"
PDF_EMPTY_MESSAGE = "PDF 文件为空或没有页面，请重新导出后上传"


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
        parsed = parse_local_pdf(content)
        return parsed.text, parsed.parser_name
    if suffix == ".docx":
        document = DocxDocument(io.BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs), "python-docx"
    raise UnsupportedDocumentError(f"No parser is configured for {filename}")


def pdf_pages(content: bytes):
    try:
        reader = PdfReader(io.BytesIO(content))
        # Permission encryption can leave the opening password empty. Decrypt
        # only in memory; the uploaded bytes and stored original stay intact.
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfParsingError("password_required", "PDF 需要打开密码，请先移除打开密码后重新上传")
        if not reader.pages:
            raise PdfParsingError("empty_pdf", PDF_EMPTY_MESSAGE)
        return reader.pages
    except PdfParsingError:
        raise
    except EmptyFileError:
        raise PdfParsingError("empty_pdf", PDF_EMPTY_MESSAGE) from None
    except (PdfReadError, ValueError, KeyError, IndexError, TypeError):
        raise PdfParsingError("invalid_pdf", PDF_INVALID_MESSAGE) from None
    except NotImplementedError:
        raise PdfParsingError("unsupported_encryption", "此 PDF 的加密方式暂不支持，请重新导出后上传") from None


def parse_local_pdf(content: bytes) -> ParsedDocument:
    source_pages = pdf_pages(content)
    try:
        pages = [(index, [{"text": page.extract_text() or "", "block_type": "text"}])
                 for index, page in enumerate(source_pages, start=1)]
    except (PdfReadError, ValueError, KeyError, IndexError, TypeError):
        raise PdfParsingError("invalid_pdf", PDF_INVALID_MESSAGE) from None
    return assemble_pages(pages, "pypdf")
