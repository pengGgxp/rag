from contextlib import contextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent import AgentCancelled, run_retrieval_agent, stream_retrieval_agent
from app.llm import ToolCall, ToolResponse


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

    class DB:
        def get(self, model, identifier):
            return SimpleNamespace(name="Allowed") if identifier == allowed else None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    @contextmanager
    def factory():
        yield DB()

    chunk_id = uuid4()
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
            text="needle evidence",
            char_start=0,
            char_end=14,
            heading_path=None,
            page_number=2,
            source_locations=[],
            version_group_id=None,
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

    class DB:
        def get(self, model, identifier):
            return SimpleNamespace(name="Allowed") if identifier == allowed else None

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


def test_history_query_and_tool_candidates_are_not_truncated(runtime):
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i) * 5000} for i in range(18)]
    long_query = "needle " * 1000
    runtime.arguments.update(history=history, query=long_query)
    chunks = []
    for i in range(30):
        chunks.append(SimpleNamespace(
            chunk=SimpleNamespace(
                id=uuid4(), text=f"candidate {i}", char_start=i, char_end=i + 1,
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
                assert str(runtime.allowed) in messages[0]["content"]
            if self.count <= 2:
                return ToolResponse(content=None, tool_calls=[
                    search(runtime.allowed, long_query, top_k=30),
                ])
            result = json.loads(messages[-1]["content"])
            assert len(result["evidence"]) == 30
            assert "has_sufficient_evidence" not in result
            assert result["evidence"][0]["vector_distance"] == 0.95
            assert result["evidence"][-1]["version_number"] == 3
            return ToolResponse(content="I decide how to use these candidates.", tool_calls=[])

    run = run_retrieval_agent(**runtime.arguments, provider=Provider())
    assert len(run.citations) == 30
    assert len({item["chunk_id"] for item in run.citations}) == 30


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
    assert run.tool_results == [{"error": "该工具不可用"}]


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
