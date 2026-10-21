from types import SimpleNamespace

import pytest

from app.config import Settings
from app.docmind import AliyunDocMind, DocMindError, normalize_layouts
from app.parsed_document import parsed_chunks


def configured(**kwargs):
    return Settings(_env_file=None, aliyun_access_key_id="test-id",
                    aliyun_access_key_secret="test-secret", **kwargs)


def response(data, code="200"):
    return SimpleNamespace(body=SimpleNamespace(data=data, code=code))


def layout(page=0, text="Contract term is one year.", index=0, **kwargs):
    return {
        "pageNum": page, "index": index, "type": "text", "text": text,
        "pos": [{"x": 10, "y": 20}, {"x": 100, "y": 50}], **kwargs,
    }


def test_stream_upload_is_bounded_and_does_not_enable_vlm():
    calls = []

    def submit(request, runtime):
        calls.append(request)
        assert request.file_url_object.read() == b"private pdf"
        assert runtime.autoretry is False
        return response(SimpleNamespace(id="remote-job"))

    provider = AliyunDocMind(configured(docmind_max_pages=2), SimpleNamespace(
        submit_doc_parser_job_advance=submit,
    ))
    assert provider.submit(b"private pdf", 2) == "remote-job"
    assert calls[0].page_index == "1-2"
    assert calls[0].output_format == ["markdown", "visualLayoutInfo"]
    assert calls[0].llm_enhancement is False
    assert calls[0].formula_enhancement is False
    with pytest.raises(DocMindError, match="页数"):
        provider.submit(b"private pdf", 3)
    assert len(calls) == 1


def test_polling_and_pagination_do_not_drop_layouts(monkeypatch):
    statuses = iter(["Processing", "Success"])
    offsets = []
    monkeypatch.setattr("app.docmind.sleep", lambda _: None)

    def get_result(request, runtime):
        offsets.append(request.layout_num)
        count = 100 if request.layout_num == 0 else 2
        return response({"layouts": [
            layout(text=f"block {request.layout_num + i}", index=request.layout_num + i)
            for i in range(count)
        ]})

    client = SimpleNamespace(
        query_doc_parser_status_with_options=lambda *_: response(SimpleNamespace(status=next(statuses))),
        get_doc_parser_result_with_options=get_result,
    )
    parsed = AliyunDocMind(configured(), client).collect("remote-job", 1, lambda: None)
    assert offsets == [0, 100]
    assert "block 101" in parsed.text
    assert len(parsed.pages[0]["blocks"]) == 102


def test_normalization_preserves_tables_pages_offsets_and_coordinates():
    parsed = normalize_layouts([
        layout(1, index=3, type="table", markdownContent="| Fee | USD |\n| --- | --- |\n| Service | 100 |"),
        layout(0, index=1, uniqueId="first"),
        layout(0, index=1, uniqueId="first"),
    ], 2)
    chunks = parsed_chunks(parsed)
    assert len(chunks) == 2
    assert chunks[0][1][0]["page_number"] == 1
    assert chunks[1][1][0]["page_number"] == 2
    assert chunks[1][1][0]["bbox"] == [10.0, 20.0, 100.0, 50.0]
    assert "| Service | 100 |" in chunks[1][0].text
    for draft, locations in chunks:
        assert parsed.text[draft.char_start:draft.char_end] == draft.text
        assert all(row["page_number"] == locations[0]["page_number"] for row in locations)


def test_invalid_pages_and_empty_evidence_are_not_silently_accepted():
    with pytest.raises(DocMindError, match="页码"):
        normalize_layouts([layout(2)], 2)
    with pytest.raises(DocMindError, match="可检索文字"):
        normalize_layouts([layout(text="", type="image", markdownContent="![image](https://private.invalid)")], 1)


def test_provider_errors_never_expose_credentials_or_signed_urls():
    class CloudError(Exception):
        code = "Forbidden.NoPermission"

    def denied(*_):
        raise CloudError("test-secret https://private.invalid?Signature=secret")

    provider = AliyunDocMind(configured(), SimpleNamespace(submit_doc_parser_job_advance=denied))
    with pytest.raises(DocMindError, match="鉴权") as caught:
        provider.submit(b"pdf", 1)
    assert "test-secret" not in str(caught.value)
    assert "Signature" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not caught.value.uncertain


def test_network_submission_error_is_marked_uncertain():
    def timeout(*_):
        raise TimeoutError("test-secret")

    provider = AliyunDocMind(configured(), SimpleNamespace(submit_doc_parser_job_advance=timeout))
    with pytest.raises(DocMindError) as caught:
        provider.submit(b"pdf", 1)
    assert caught.value.uncertain


def test_timeout_keeps_known_task_available_for_retry(monkeypatch):
    ticks = iter([0, 2])
    monkeypatch.setattr("app.docmind.monotonic", lambda: next(ticks))
    with pytest.raises(DocMindError, match="继续查询原任务"):
        AliyunDocMind(configured(docmind_timeout_seconds=1), SimpleNamespace()).collect(
            "remote-job", 1, lambda: None,
        )


def test_missing_credentials_are_rejected():
    with pytest.raises(DocMindError, match="密钥"):
        AliyunDocMind(Settings(
            _env_file=None,
            aliyun_access_key_id=None,
            aliyun_access_key_secret=None,
        ))
