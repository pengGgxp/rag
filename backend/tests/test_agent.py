from contextlib import contextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.agent.loop import AgentCancelled, run_retrieval_agent, stream_retrieval_agent
from app.model_runtime.client import ToolCall, ToolResponse
from app.models import DocumentStatus, KnowledgeBaseStatus


def test_retrieval_agent_only_searches_allowed_knowledge_bases():
    allowed = uuid4()
    outside = uuid4()
    calls = []

    class Provider:
        step = 0

        def complete_with_tools(self, messages, tools, tool_choice):
            assert tool_choice == "auto"
            self.step += 1
            if self.step == 3:
                return ToolResponse(content="done", tool_calls=[])
            return ToolResponse(
                content=None,
                tool_calls=[ToolCall(
                    id="call-1",
                    name="search_knowledge_base",
                    arguments={
                        "knowledge_base_ids": (
                            [str(outside), str(allowed)] if self.step == 1 else [str(allowed)]
                        ),
                        "query": "needle",
                    },
                )],
            )

    class Access:
        def revalidate(self, db):
            return self

        def library(self, db, identifier):
            if identifier != allowed:
                raise HTTPException(404, "not authorized")
            return SimpleNamespace(id=allowed, name="Allowed", status=KnowledgeBaseStatus.READY)

    class DB:
        def get(self, model, identifier, **kwargs):
            return SimpleNamespace(name="Allowed") if identifier == allowed else None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    @contextmanager
    def factory():
        yield DB()

    chunk_id = uuid4()
    document_id = uuid4()
    version_group_id = uuid4()
    trace = SimpleNamespace(
        id=uuid4(),
        results=[SimpleNamespace(
            chunk_id=chunk_id,
            lexical_rank=1,
            vector_distance=None,
        )],
    )
    result = SimpleNamespace(
        chunk=SimpleNamespace(
            id=chunk_id,
            document_id=document_id,
            text="needle evidence",
            char_start=0,
            char_end=14,
            heading_path=None,
            page_number=2,
            source_locations=[],
            version_group_id=version_group_id,
            version_number=1,
        ),
        filename="guide.md",
        score=0.9,
    )

    def retrieve(knowledge_base_id, query, db, access, *, result_limit):
        calls.append((knowledge_base_id, query))
        return SimpleNamespace(trace=trace, results=[result])

    run = run_retrieval_agent(
        query="needle",
        knowledge_base_ids=[allowed],
        history=[],
        factory=factory,
        access=Access(),
        retrieve=retrieve,
        provider=Provider(),
    )

    assert calls == [(allowed, "needle")]
    assert len(run.citations) == 1
    assert run.citations[0]["page_number"] == 2
    assert run.citations[0]["document_id"] == str(document_id)
    assert run.citations[0]["version_id"] == str(document_id)
    assert "error" in run.tool_results[0]
    assert run.tool_results[1]["result_count"] == 1
    assert run.answer == "done"


@pytest.fixture
def runtime():
    allowed = uuid4()
    calls = []
    active = {"value": True}

    class Access:
        def revalidate(self, db):
            return self

        def library(self, db, identifier):
            if identifier != allowed:
                raise HTTPException(404, "not authorized")
            return SimpleNamespace(id=allowed, name="Allowed", status=KnowledgeBaseStatus.READY)

    class DB:
        def get(self, model, identifier, **kwargs):
            if model.__name__ == "Document":
                return SimpleNamespace(
                    id=identifier, knowledge_base_id=allowed, status=DocumentStatus.READY, is_active=True,
                )
            return SimpleNamespace(id=allowed, name="Allowed", status=KnowledgeBaseStatus.READY) if identifier == allowed else None

    @contextmanager
    def factory():
        yield DB()

    def retrieve(identifier, query, db, access, *, result_limit):
        calls.append((identifier, query, result_limit))
        return SimpleNamespace(
            trace=SimpleNamespace(id=uuid4(), results=[]), results=[],
        )

    return SimpleNamespace(
        allowed=allowed, calls=calls, active=active,
        arguments=dict(
            query="question", knowledge_base_ids=[allowed], history=[],
            factory=factory, access=Access(), retrieve=retrieve,
            is_active=lambda: active["value"],
        ),
    )


def search(identifier, query="needle", **kwargs):
    return ToolCall(
        id=str(uuid4()), name="search_knowledge_base",
        arguments={"knowledge_base_ids": [str(identifier)], "query": query, **kwargs},
    )


def test_mention_is_optional_and_only_applies_when_model_selects_document(runtime):
    document_id = uuid4()
    runtime.arguments["document_mentions"] = [{"id": str(document_id), "filename": "guide.pdf",
        "knowledge_base_id": str(runtime.allowed)}]
    calls = []

    def retrieve(identifier, query, db, access, *, result_limit, document_ids=None):
        calls.append(document_ids)
        return SimpleNamespace(trace=SimpleNamespace(id=uuid4(), results=[]), results=[])

    runtime.arguments["retrieve"] = retrieve

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            assert str(document_id) in messages[0]["content"]
            if self.count == 1:
                return ToolResponse(content=None, tool_calls=[search(runtime.allowed)])
            if self.count == 2:
                return ToolResponse(content=None, tool_calls=[search(runtime.allowed,
                    document_ids=[str(document_id)])])
            return ToolResponse(content="done", tool_calls=[])

    run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert calls == [None, [document_id]]


@pytest.mark.parametrize("document_ids", [[], ["invalid"], "invalid", [str(uuid4())] * 9])
def test_invalid_documents_cannot_be_used_as_tool_scope(runtime, document_ids):
    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                return ToolResponse(content=None, tool_calls=[search(runtime.allowed, document_ids=document_ids)])
            assert "error" in json.loads(messages[-1]["content"])
            return ToolResponse(content="done", tool_calls=[])

    run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert runtime.calls == []


def test_model_can_continue_past_old_and_framework_round_limits(runtime):
    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 41:
                assert len([m for m in messages if m["role"] == "tool"]) == 160
                return ToolResponse(content="Can you clarify the document name?", tool_calls=[])
            return ToolResponse(
                content=None,
                tool_calls=[search(runtime.allowed, f"attempt {self.count}-{i}") for i in range(4)],
            )

    run = run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert run.steps == 41
    assert len(runtime.calls) == len(run.tool_calls) == len(run.tool_results) == 160
    assert run.citations == []
    assert run.answer == "Can you clarify the document name?"


def test_history_and_query_preserved_while_evidence_is_bounded_as_whole_chunks(runtime):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i) * 5000} for i in range(18)]
    long_query = "needle " * 1000
    runtime.arguments.update(history=history, query=long_query)
    chunks = []
    for i in range(30):
        chunks.append(SimpleNamespace(
            chunk=SimpleNamespace(
                id=uuid4(), document_id=uuid4(), text=f"candidate {i}", char_start=i, char_end=i + 1,
                heading_path=None, page_number=2, source_locations=[],
                version_group_id=uuid4(), version_number=3,
            ),
            filename="report.pdf", score=1 / (i + 1),
        ))

    def retrieve(identifier, query, db, access, *, result_limit):
        assert query == long_query
        assert result_limit == 30
        return SimpleNamespace(
            results=chunks,
            trace=SimpleNamespace(
                id=uuid4(),
                results=[SimpleNamespace(
                    chunk_id=item.chunk.id, lexical_rank=None, vector_distance=0.95,
                ) for item in chunks],
            ),
        )

    runtime.arguments["retrieve"] = retrieve

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                assert messages[1:-1] == history
                assert messages[-1]["content"] == long_query
                assert any(
                    tool["function"]["name"] == "discover_knowledge_bases"
                    for tool in tools
                )
            if self.count <= 2:
                return ToolResponse(content=None, tool_calls=[
                    search(runtime.allowed, long_query, top_k=30),
                ])
            result = json.loads(messages[-1]["content"])
            assert 0 < len(result["evidence"]) < 30
            assert result["omitted_count"] == 30 - len(result["evidence"])
            assert "has_sufficient_evidence" not in result
            assert result["evidence"][0]["vector_distance"] == 0.95
            assert result["evidence"][-1]["version_number"] == 3
            return ToolResponse(content="I decide how to use these candidates.", tool_calls=[])

    run = run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert 0 < len(run.citations) < 30
    assert len({item["chunk_id"] for item in run.citations}) == len(run.citations)
    assert all(item["text"] == f"candidate {i}" for i, item in enumerate(run.citations))


@pytest.mark.parametrize("arguments", [
    {"knowledge_base_ids": []},
    {"knowledge_base_ids": [str(uuid4())]},
    {"knowledge_base_ids": ["invalid"]},
    {"query": ""},
    {"query": {"invalid": "object"}},
    {"top_k": 0},
    {"top_k": True},
    {"top_k": "20"},
])
def test_invalid_arguments_return_errors_without_silent_fallback(runtime, arguments):
    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                call = search(runtime.allowed)
                call.arguments.update(arguments)
                return ToolResponse(content=None, tool_calls=[call])
            assert "error" in json.loads(messages[-1]["content"])
            return ToolResponse(content="Please choose an available library.", tool_calls=[])

    run = run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert runtime.calls == []
    assert run.answer == "Please choose an available library."


def test_unknown_tool_returns_error_to_model(runtime):
    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                return ToolResponse(content=None, tool_calls=[
                    ToolCall(id="unknown", name="delete_document", arguments={}),
                ])
            assert "error" in json.loads(messages[-1]["content"])
            return ToolResponse(content="I cannot delete documents.", tool_calls=[])

    run = run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert runtime.calls == []
    assert run.tool_results[0]["error"] == "该工具不可用"
    assert run.tool_results[0]["status"] == "error"


def test_file_disappears_then_model_uses_another_path_in_same_turn(runtime):
    class Extensions:
        tools, skills = [], []
        paths = []
        def label(self, *args):
            return "读取文件"
        def execute(self, name, arguments, **kwargs):
            self.paths.append(arguments["path"])
            if arguments["path"] == "old.txt":
                raise HTTPException(404, "文件 old.txt 不存在")
            return {"text": "当前文件内容"}
    extensions = Extensions()
    class Provider:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 2:
                assert json.loads(messages[-1]["content"])["status_code"] == 404
                assert "old.txt" in messages[-1]["content"]
            if self.step == 3:
                assert json.loads(messages[-1]["content"])["text"] == "当前文件内容"
                return ToolResponse("已找到改名后的文件", [])
            return ToolResponse(None, [ToolCall(str(self.step), "read_file",
                {"path": "old.txt" if self.step == 1 else "new.txt"})])
    run = run_retrieval_agent(**runtime.arguments, provider=Provider(), extensions=extensions)
    assert extensions.paths == ["old.txt", "new.txt"]
    assert run.answer == "已找到改名后的文件"


@pytest.mark.parametrize("malformed", [True, False])
def test_argument_feedback_is_corrected_without_replaying_tool(runtime, malformed):
    executions = []

    class Extensions:
        tools, skills = [], []

        def label(self, *args):
            return "生成文件"

        def execute(self, name, arguments, **kwargs):
            executions.append(arguments)
            return ({"error": "path 必须是文本", "error_code": "file_arguments"}
                if not isinstance(arguments.get("path"), str) else {"summary": "文件已保存"})

    class Provider:
        step = 0

        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                call = ToolCall.parse("bad", "write_file", '{"path":' if malformed else '{"path": 2}')
                return ToolResponse(None, [call])
            if self.step == 2:
                assert json.loads(messages[-1]["content"])["error"]
                return ToolResponse(None, [ToolCall("fixed", "write_file", {"path": "result.md"})])
            assert json.loads(messages[-1]["content"]) == {"summary": "文件已保存"}
            return ToolResponse("已生成文件。", [])

    events = list(stream_retrieval_agent(**runtime.arguments, provider=Provider(), extensions=Extensions()))
    results = [value for kind, value in events if kind == "tool_result"]
    assert [value["outcome"] for value in results] == ["feedback", "success"]
    assert [value["status"] for value in results] == ["error", "completed"]
    assert len(executions) == (1 if malformed else 2)
    assert executions[-1] == {"path": "result.md"}
    assert events[-1][0] == "complete" and events[-1][1].answer == "已生成文件。"


def test_retrieval_http_failure_is_tool_observation_not_turn_failure(runtime):
    def unavailable(*args, **kwargs):
        raise HTTPException(409, "知识库已归档")
    class Provider:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [search(runtime.allowed)])
            result = json.loads(messages[-1]["content"])
            assert result["error"] == "知识库已归档" and result["evidence"] == []
            return ToolResponse("请提供其他资料", [])
    run = run_retrieval_agent(**{**runtime.arguments, "retrieve": unavailable}, provider=Provider())
    assert run.answer == "请提供其他资料" and run.tool_results[0]["status"] == "error"


@pytest.mark.parametrize("name,result,expected", [
    ("write_file", {"error": "目标文件有未完成的操作", "error_code": "file_storage"}, "目标文件有未完成的操作"),
    ("write_file", {"error": "上级路径是文件", "error_code": "file_conflict"}, "上级路径是文件"),
    ("run_command", {"error": "该文件不存在", "error_code": "file_tool_unavailable"}, "该文件不存在"),
    ("run_command", {"error": "执行结果尚未确认，请核对文件，勿重复运行", "uncertain": True}, "勿重复运行"),
    ("mcp_remote", {"error": "服务返回目标不存在", "error_code": "file_storage"}, "服务返回目标不存在"),
])
def test_file_failure_stream_uses_safe_actionable_summary(runtime, name, result, expected):
    class Extensions:
        tools = []
        skills = []

        def label(self, *args):
            return "生成文件"

        def execute(self, *args, **kwargs):
            return result

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            return (ToolResponse(content=None, tool_calls=[ToolCall(id="file", name=name, arguments={})])
                if self.count == 1 else ToolResponse(content="Unable to complete.", tool_calls=[]))

    events = list(stream_retrieval_agent(**runtime.arguments, provider=Provider(), extensions=Extensions()))
    event = next(value for kind, value in events if kind == "tool_result")
    assert expected in event["summary"] and "PRIVATE" not in event["summary"]
    assert "检查连接或授权" not in event["summary"]


def test_successful_file_output_is_attached_to_tool_activity_and_prompt(runtime):
    from app.agent.loop import AGENT_SYSTEM_PROMPT

    output = {"id": "file-1", "path": "reports/summary.md", "size_bytes": 21, "version": 1,
        "object_key": "must-not-be-public"}

    class Extensions:
        tools = []
        skills = []
        environment_description = "verified-environment-for-test"

        def label(self, *args):
            return "生成文件"

        def execute(self, *args, **kwargs):
            return {"path": output["path"], "generated_files": [output]}

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                assert "[文件名](工具返回的url)" in messages[0]["content"]
                assert "该次工具活动下方的下载入口" not in messages[0]["content"]
                assert "verified-environment-for-test" in messages[0]["content"]
                return ToolResponse(content=None, tool_calls=[ToolCall(id="file", name="run_command", arguments={})])
            return ToolResponse(content="已生成 reports/summary.md。", tool_calls=[])

    events = list(stream_retrieval_agent(**runtime.arguments, provider=Provider(), extensions=Extensions()))
    event = next(value for kind, value in events if kind == "tool_result")
    assert event["summary"] == "已生成 1 个文件"
    assert event["files"] == [{key: output[key] for key in ("id", "path", "size_bytes", "version")}]
    assert "object_key" not in json.dumps(event)
    assert "不要编造 URL 或下载地址" in AGENT_SYSTEM_PROMPT
    assert "文件存在、读取成功、取链接成功、命令退出成功或预览就绪等同于成果质量已验证" in AGENT_SYSTEM_PROMPT
    assert "同时说明脚本未完成并列出这些文件" in AGENT_SYSTEM_PROMPT
    assert "不要向用户原样展示 queued、processing、ready 等内部状态码" in AGENT_SYSTEM_PROMPT
    assert "不规定固定工具顺序" in AGENT_SYSTEM_PROMPT
    assert "远程图片" in AGENT_SYSTEM_PROMPT and "run_command" in AGENT_SYSTEM_PROMPT


@pytest.mark.parametrize(("name", "result"), [
    ("write_file", {"id": "file-1", "path": "报告.md", "size_bytes": 21,
        "download_url": "/resources/file-1/download"}),
    ("start_preview", {"preview": {"status": "ready", "name": "排序演示",
        "url": "https://example.test/previews/preview-1"}}),
    ("start_preview", {"error": "网页未就绪", "preview": {"status": "failed", "url": None}}),
    ("generate_image", {"task_id": "image-1", "status": "queued", "files": [],
        "summary": "等待生成", "view_url": "/resources"}),
    ("edit_image", {"task_id": "image-2", "status": "completed",
        "files": [{"id": "file-2", "path": "修改后的图片.png"}],
        "summary": "图片已生成", "view_url": "/resources"}),
    ("run_command", {"error": "脚本部分失败", "generated_files": [
        {"id": "file-3", "path": "报告.pdf", "size_bytes": 30}]}),
    ("save_to_knowledge_base", {"save_id": "save-1", "document_id": "document-1",
        "status": "processing", "summary": "资料正在处理",
        "url": "/?library=library-1&view=documents&document=document-1"}),
])
def test_delivery_instructions_and_actual_results_reach_final_model_round(runtime, name, result):
    class Extensions:
        tools = []
        skills = []

        def label(self, *args):
            return name

        def execute(self, *args, **kwargs):
            return result

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                return ToolResponse(content=None, tool_calls=[ToolCall(id="deliver", name=name, arguments={})])
            prompt = messages[0]["content"]
            assert "工具执行结束后必须用自然语言给出简短交付总结" in prompt
            assert "get_workspace_file_link" in prompt and "[文件名](url)" in prompt
            assert "[打开资源库](/resources)" not in prompt
            assert "preview.status 为 ready 且 preview.url 存在" in prompt
            assert "download_url 等 API 下载地址" in prompt
            assert "尚未完成及可在哪里查看进度" in prompt
            assert "部分完成、失败、取消或结果尚未确认" in prompt
            assert json.loads(next(item["content"] for item in reversed(messages)
                if item["role"] == "tool")) == result
            return ToolResponse(content="交付说明测试。", tool_calls=[])

    provider = Provider()
    run = run_retrieval_agent(**runtime.arguments, provider=provider, extensions=Extensions())
    assert provider.count == 2
    assert run.answer == "交付说明测试。"


@pytest.mark.parametrize("state", ["starting", "ready", "ended"])
def test_cloud_file_results_reach_model_and_allow_remaining_tools(runtime, state):
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.cloud_computer.runtime import read

    timestamp = datetime(2026, 11, 6, 13, 17, 13, tzinfo=timezone.utc)
    computer = SimpleNamespace(id=uuid4(), name="测试实例", conversation_id=uuid4(), status=state, error=None,
        started_at=None if state == "starting" else timestamp,
        ended_at=timestamp if state == "ended" else None, billing_stopped_at=None,
        stop_reason=None,
        last_activity_at=timestamp, charged_credits=Decimal("0"), held_credits=Decimal("20"),
        snapshot={"credits_per_minute": "2", "max_seconds": 3600, "idle_seconds": 600})
    completed = []

    class Extensions:
        tools = []
        skills = []

        def label(self, name, arguments):
            return name

        def execute(self, name, arguments, **kwargs):
            completed.append(name)
            return {"status": "completed", "cloud_computer": read(computer)}

    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            if self.count == 1:
                return ToolResponse(content=None, tool_calls=[
                    ToolCall(id="write", name="write_file", arguments={}),
                    ToolCall(id="run", name="run_command", arguments={}),
                ])
            results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
            assert len(results) == 2
            for result in results:
                value = result["cloud_computer"]
                assert value["started_at"] == (None if state == "starting" else timestamp.isoformat())
                assert value["ended_at"] == (timestamp.isoformat() if state == "ended" else None)
            return ToolResponse(content="File saved.", tool_calls=[])

    events = list(stream_retrieval_agent(**runtime.arguments, provider=Provider(), extensions=Extensions()))
    assert completed == ["write_file", "run_command"]
    assert [data["status"] for name, data in events if name == "tool_result"] == ["completed", "completed"]
    assert any(name == "complete" for name, _ in events)


def test_stop_between_rounds_prevents_additional_model_and_tool_calls(runtime):
    class Provider:
        count = 0

        def complete_with_tools(self, messages, tools, *, tool_choice):
            self.count += 1
            return ToolResponse(content=None, tool_calls=[search(runtime.allowed)])

    provider = Provider()
    execution = stream_retrieval_agent(**runtime.arguments, provider=provider)
    assert next(execution)[0] == "agent_step"
    assert next(execution)[0] == "tool_call"
    assert next(execution)[0] == "tool_started"
    assert next(execution)[0] == "tool_result"
    runtime.active["value"] = False
    with pytest.raises(AgentCancelled):
        next(execution)
    assert provider.count == len(runtime.calls) == 1


def test_stop_during_model_request_discards_late_tool_calls(runtime):
    class Provider:
        def complete_with_tools(self, messages, tools, *, tool_choice):
            runtime.active["value"] = False
            return ToolResponse(content=None, tool_calls=[search(runtime.allowed)])

    with pytest.raises(AgentCancelled):
        run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert runtime.calls == []
