"""The isolated OCR evaluator never touches business billing or model APIs."""

from dataclasses import asdict
import json

import pytest

pytest.importorskip("markitdown")
from app.parsed_document import ParsedDocument
from experiments import benchmark
from experiments.checkpoints import Checkpoints, UncertainCall
from experiments.selective_parser import image_pdf


def test_fact_location_does_not_borrow_neighbouring_source():
    parsed = ParsedDocument("alpha\n\nbeta", "test", source_locations=[
        {"char_start": 0, "char_end": 5, "block_type": "paragraph"},
        {"char_start": 7, "char_end": 11, "block_type": "table"},
    ])
    result = benchmark.metrics({"facts": ["alpha"], "expected_locations": [{"block_type": "table"}]},
                               {"parsed": asdict(parsed)})
    assert result["facts_found"] == 1
    assert result["source_correct"] == 0
    assert "questions" not in result


def test_completed_ocr_reuses_result_and_uncertain_submission_never_replays(tmp_path, monkeypatch):
    checkpoints = Checkpoints(tmp_path / "costs.sqlite")
    rate = {"source": "verified tariff", "verified_at": "2026-10-30", "upper_yuan_per_page": ".25"}
    checkpoints.reserve("docmind-v1:complete", "ocr", ".25")
    checkpoints.finish("docmind-v1:complete", asdict(ParsedDocument("saved", "test")), 2)
    checkpoints.reserve("docmind-v1:uncertain", "ocr", ".25")
    def forbidden():
        pytest.fail("Must not construct a cloud client or replay a paid submission")
    monkeypatch.setattr(benchmark, "AliyunDocMind", forbidden)
    benchmark.fulfill({"hash": "complete", "pages": 1}, tmp_path, checkpoints, rate)
    assert json.loads((tmp_path / "ocr-results/complete.json").read_text())["text"] == "saved"
    with pytest.raises(UncertainCall):
        benchmark.fulfill({"hash": "uncertain", "pages": 1}, tmp_path, checkpoints, rate)
    assert checkpoints.summary()["reserved_upper_yuan"] == .5


def test_submitted_task_resumes_polling_without_new_submission(tmp_path, monkeypatch):
    checkpoints = Checkpoints(tmp_path / "costs.sqlite")
    checkpoints.reserve("docmind-v1:known", "ocr", ".25")
    checkpoints.update("docmind-v1:known", state="submitted", task_id="task-1")
    class Provider:
        def submit(self, *_):
            pytest.fail("Already submitted task must not be submitted again")
        def collect(self, task, pages, progress):
            assert task == "task-1" and pages == 1
            return ParsedDocument("resumed", "test")
    monkeypatch.setattr(benchmark, "AliyunDocMind", Provider)
    benchmark.fulfill({"hash": "known", "pages": 1}, tmp_path, checkpoints,
                      {"source": "verified", "verified_at": "2026-10-30", "upper_yuan_per_page": ".25"})
    assert checkpoints.get("docmind-v1:known")["state"] == "complete"


def test_image_pdf_hash_is_stable_across_time():
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new("RGB", (30, 30), "white").save(stream, "PNG")
    first = image_pdf(stream.getvalue())
    assert first == image_pdf(stream.getvalue())
    assert b"CreationDate (D:" not in first
