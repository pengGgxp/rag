"""Run with requirements-parsing-experiment.txt; ordinary CI can skip the experiment."""
import io
import zipfile

import pytest

pytest.importorskip("markitdown")
from docx import Document
from docx.shared import Inches
from PIL import Image
from app.knowledge.ingestion.parsed_document import ParsedDocument, parsed_chunks
from experiments.selective_parser import parse_docx, parse_pdf, Policy, page_decision, _marked_docx
from experiments.fixtures import generate, pdf_document, screenshot


def doc_bytes(document):
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def no_ocr(*args):
    raise AssertionError("No paid call expected")


def test_word_structure_and_locations_without_ocr():
    doc = Document()
    doc.add_heading("Project", level=1)
    doc.add_paragraph("Before table")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Amount", "840"
    doc.add_paragraph("After table")
    result = parse_docx(doc_bytes(doc), no_ocr)
    assert result.complete and "840" in result.parsed.text
    assert result.parsed.text.index("Before") < result.parsed.text.index("840") < result.parsed.text.index("After")
    assert any(source["block_type"] == "table" for source in result.parsed.source_locations)
    assert all("page_number" not in source for source in result.parsed.source_locations)
    assert any(locations for _, locations in parsed_chunks(result.parsed))


def test_images_keep_position_and_deduplicate():
    raw = io.BytesIO()
    Image.new("RGB", (100, 80), "white").save(raw, "PNG")
    doc = Document()
    doc.add_paragraph("Before")
    doc.add_picture(io.BytesIO(raw.getvalue()), width=Inches(1))
    doc.add_paragraph("Between")
    doc.add_picture(io.BytesIO(raw.getvalue()), width=Inches(1))
    doc.add_paragraph("After")
    calls = []
    def ocr(data, pages, purpose):
        assert data.startswith(b"%PDF") and pages == 1
        calls.append(purpose)
        return ParsedDocument("Image fact 721", "test")
    result = parse_docx(doc_bytes(doc), ocr)
    assert len(calls) == 1 and len(result.decisions) == 2
    assert result.parsed.text.count("Image fact 721") == 2
    assert result.parsed.text.index("Before") < result.parsed.text.index("Image fact") < result.parsed.text.index("Between")
    assert len([s for s in result.parsed.source_locations if s["block_type"] == "image"]) == 2
    assert "ZKIMAGE" not in result.parsed.text


def test_ocr_failure_retains_partial_content():
    raw = io.BytesIO()
    Image.new("RGB", (100, 80), "white").save(raw, "PNG")
    doc = Document()
    doc.add_paragraph("Keep original text")
    doc.add_picture(io.BytesIO(raw.getvalue()), width=Inches(1))
    result = parse_docx(doc_bytes(doc), no_ocr)
    assert not result.complete and result.warnings
    assert "Keep original text" in result.parsed.text
    assert "AssertionError" not in result.parsed.text


def test_docx_rejects_expansion_and_entities(monkeypatch):
    import experiments.selective_parser as parser
    doc = doc_bytes(Document())
    monkeypatch.setattr(parser, "MAX_EXPANDED", 10)
    with pytest.raises(ValueError, match="expanded"):
        _marked_docx(doc)


def test_mixed_pdf_calls_only_scan_page_and_keeps_original_number(tmp_path):
    path = tmp_path / "mixed.pdf"
    pdf_document(path, [{"lines": ["Project 123: directly readable native text", "Budget 456, owner John Smith"]},
                        {"image": screenshot(["Scanned project 789"])}])
    calls = []
    def ocr(data, pages, purpose):
        calls.append(purpose)
        return ParsedDocument("Scanned project 789", "test")
    result = parse_pdf(path.read_bytes(), ocr)
    assert result.complete and calls == ["pdf-page-2"]
    assert [d["route"] for d in result.decisions] == ["local", "ocr"]
    assert result.parsed.pages[1]["page_number"] == 2
    assert result.parsed.text.count("Scanned project 789") == 1


def test_fixture_manifest_has_twelve_cases_and_independent_calibration(tmp_path):
    samples = generate(tmp_path)
    assert len(samples) == 12 and all(len(s["facts"]) == 2 for s in samples)
    assert not any("calibration" in s["path"] for s in samples)
