import csv

from experiments.report import render


def test_report_escapes_embedded_data_and_uses_logical_ocr_pages(tmp_path):
    metrics = {"facts_found": 0, "fact_count": 0, "source_correct": 0, "ordered": False}
    result = {"metrics": metrics, "complete": True, "parsed": {"pages": []}, "local_seconds": 1,
              "ocr_pages": 2, "ocr_upper_yuan": .5, "recorded_ocr_seconds": 3}
    payload = {"note": "</script><script>alert('private')</script>", "results": [
        {"sample": {"id": "test", "path": "sample.docx", "kind": "word"}, "arms": {"candidate": result}}]}
    render(payload, tmp_path)
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "</script><script>alert" not in report
    assert "\\u003c/script>" in report
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["ocr_pages"] == "2" and row["ocr_upper_yuan"] == "0.5"
    assert "questions" not in row
