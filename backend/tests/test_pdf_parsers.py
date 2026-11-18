import hashlib
import io

import pytest
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app import parsers
from app.parsed_document import parsed_chunks
from pdf_fixtures import make_pdf


@pytest.mark.parametrize("algorithm", [None, "RC4-128", "AES-128", "AES-256"])
def test_plain_and_empty_password_pdf_preserve_pages_citations_and_original(algorithm):
    content = make_pdf(password="" if algorithm else None, algorithm=algorithm or "AES-128")
    digest = hashlib.sha256(content).hexdigest()
    reader = PdfReader(io.BytesIO(content))
    assert reader.is_encrypted == (algorithm is not None)
    if algorithm == "AES-128":
        assert reader.trailer["/Encrypt"]["/R"] == 4
        assert reader.trailer["/Encrypt"]["/CF"]["/StdCF"]["/CFM"] == "/AESV2"
    assert len(parsers.pdf_pages(content)) == 2
    parsed = parsers.parse_local_pdf(content)
    assert [page["page_number"] for page in parsed.pages] == [1, 2]
    for draft, locations in parsed_chunks(parsed):
        assert parsed.text[draft.char_start:draft.char_end] == draft.text
        assert f"Page {locations[0]['page_number']} evidence" in draft.text
    assert parsers.parse_document("file.pdf", "application/pdf", content) == (parsed.text, "pypdf")
    assert hashlib.sha256(content).hexdigest() == digest
    assert PdfReader(io.BytesIO(content)).is_encrypted == (algorithm is not None)


@pytest.mark.parametrize("content,code,message", [
    (make_pdf(password="secret"), "password_required", "需要打开密码"),
    (b"%PDF-1.7\ninvalid", "invalid_pdf", "已损坏或格式无效"),
    (b"not a PDF", "invalid_pdf", "已损坏或格式无效"),
    (b"", "empty_pdf", "为空或没有页面"),
    (make_pdf(page_count=0), "empty_pdf", "为空或没有页面"),
    (make_pdf(password="", page_count=0), "empty_pdf", "为空或没有页面"),
])
def test_pdf_failures_are_distinct_across_reading_and_parsing(content, code, message):
    for parse in (parsers.pdf_pages, parsers.parse_local_pdf,
                  lambda value: parsers.parse_document("file.pdf", "application/pdf", value)):
        with pytest.raises(parsers.PdfParsingError, match=message) as error:
            parse(content)
        assert error.value.code == code


def test_corrupt_page_content_is_not_reported_as_a_password_failure(monkeypatch):
    def broken(_):
        raise PdfReadError("private PDF stream contents")

    monkeypatch.setattr("pypdf._page.PageObject.extract_text", broken)
    with pytest.raises(parsers.PdfParsingError, match="已损坏或格式无效") as error:
        parsers.parse_local_pdf(make_pdf(password=""))
    assert error.value.code == "invalid_pdf"
    assert "private" not in str(error.value)


def test_blank_pages_are_valid_pdf_pages_not_corrupt_or_password_protected():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    output = io.BytesIO()
    writer.write(output)
    assert len(parsers.pdf_pages(output.getvalue())) == 1
    assert parsers.parse_local_pdf(output.getvalue()).text == ""


def test_aes128_empty_password_pdf_keeps_all_128_pages():
    content = make_pdf(password="", page_count=128)
    parsed = parsers.parse_local_pdf(content)
    assert len(parsed.pages) == 128
    assert parsed.pages[-1]["page_number"] == 128
    assert "Page 128 evidence" in parsed.text


def test_pdf_failure_round_trips_through_task_exception_serialization():
    from celery.backends.base import Backend
    from app.tasks import celery_app

    backend = Backend(app=celery_app)
    error = parsers.PdfParsingError("password_required", "PDF 需要打开密码")
    restored = backend.exception_to_python(backend.prepare_exception(error, serializer="json"))
    assert isinstance(restored, parsers.PdfParsingError)
    assert restored.code == error.code
    assert str(restored) == str(error)


def test_unsupported_encryption_is_not_called_corruption(monkeypatch):
    def unsupported(_):
        raise NotImplementedError("private encryption details")

    monkeypatch.setattr(parsers, "PdfReader", unsupported)
    with pytest.raises(parsers.PdfParsingError, match="加密方式暂不支持") as error:
        parsers.pdf_pages(b"%PDF-1.7\n")
    assert error.value.code == "unsupported_encryption"
