from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from app.agent import run_retrieval_agent
from app.llm import ToolCall, ToolResponse


def test_retrieval_agent_only_searches_allowed_knowledge_bases():
    allowed = uuid4()
    outside = uuid4()
    calls = []

    class Provider:
        used = False

        def complete_with_tools(self, messages, tools, tool_choice):
            assert tool_choice == "auto"
            if self.used:
                return ToolResponse(content="done", tool_calls=[])
            self.used = True
            return ToolResponse(
                content=None,
                tool_calls=[ToolCall(
                    id="call-1",
                    name="search_knowledge_base",
                    arguments={
                        "knowledge_base_ids": [str(outside), str(allowed)],
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

    def retrieve(knowledge_base_id, query, db, access):
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
    assert run.tool_results[0]["result_count"] == 1
    assert run.answer == "done"
