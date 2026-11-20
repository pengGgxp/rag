import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.agent import run_retrieval_agent
from app.agent_tools import bounded_message
from app.llm import ToolCall, ToolResponse
from app.models import Chunk, Document, DocumentStatus, KnowledgeBase, MCPServer
from test_agent_continuation import assistant, client, fresh_manager, tool_message


def saved(manager, name, data, key="saved"):
    call = ToolCall(key, name, {})
    manager.record([assistant([call])], "model", "model:" + key)
    identifier = manager.begin_tool(call)
    visible = manager.finish_tool(identifier, tool_message(call, data), 1600)
    return identifier, json.loads(visible["content"])


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_model_reads_web_tail_by_literal_unicode_offset_without_new_network(client, protocol):
    db, manager = fresh_manager(client)
    manager.config = replace(manager.config, api_protocol=protocol)
    text = "开头😀" * 20000 + "尾部专属事实：三十八"
    seen = []
    class Search:
        tools = []
        def execute(self, name, arguments, *args):
            seen.append(name)
            return {"content": text, "source_id": "web-snapshot", "title": "Page",
                "url": "https://example.test", "site_name": "example.test", "type": "web",
                "fetched_at": "2026-11-16T00:00:00Z", "result_count": 1}
    class Model:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [ToolCall("web", "web_fetch", {"url": "https://example.test"})])
            result = json.loads(messages[-1]["content"])
            if self.step == 2:
                assert result["source_id"] == "web-snapshot" and result["truncated"]
                assert "尾部专属事实" not in result["preview"]
                return ToolResponse(None, [ToolCall("tail", "read_tool_result",
                    {"result_id": result["result_id"], "field": "content", "find": "尾部专属事实"})])
            assert result["offset"] == len("开头😀" * 20000)
            assert result["text"] == "尾部专属事实：三十八"
            assert result["fetched_at"] == "2026-11-16T00:00:00Z" and not result["truncated"]
            return ToolResponse("尾部事实已核对 [web-snapshot]", [])
    run = run_retrieval_agent(query="查询尾部", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager, search=Search())
    assert run.answer.endswith("[web-snapshot]") and seen == ["web_fetch"]


@pytest.mark.parametrize("data", [
    {"status": "failed", "error": "脚本失败", "exit_code": 17, "stdout": "x" * 50000},
    {"status": "completed", "exit_code": 0, "stdout": "x" * 50000},
    {"isError": True, "content": [{"type": "text", "text": "x" * 50000}]},
])
def test_projection_preserves_execution_truth(data):
    visible = json.loads(bounded_message({"role": "tool", "content": json.dumps(data)}, uuid4(), 1400)["content"])
    for key in ("status", "error", "exit_code", "isError"):
        if key in data:
            assert visible[key] == data[key]
    assert visible["truncated"] and visible["text_fields"]


def knowledge_candidates(db, manager):
    library = KnowledgeBase(account_id=manager.access.user_id, owner_id=manager.access.user_id, name="source")
    db.add(library); db.flush()
    document = Document(knowledge_base_id=library.id, filename="guide.txt", content_type="text/plain",
        size_bytes=1, object_key=str(uuid4()), content_hash="a" * 64, status=DocumentStatus.READY)
    db.add(document); db.flush()
    candidates = []
    for index in range(12):
        chunk = Chunk(document_id=document.id, ordinal=index, text=str(index) + "资料" * 100,
            token_count=500, char_start=index * 1001, char_end=(index + 1) * 1001, content_hash="b" * 64)
        db.add(chunk); db.flush()
        candidates.append({"chunk_id": str(chunk.id), "knowledge_base_id": str(library.id),
            "version_id": str(document.id), "document_id": str(document.version_group_id),
            "text": chunk.text, "char_start": chunk.char_start, "char_end": chunk.char_end, "source_locations": []})
    db.commit()
    return document, candidates


@pytest.mark.parametrize("change", ["delete", "replace", "archive"])
def test_candidate_pages_revalidate_versions_and_inherit_access_on_saved_pages(client, change):
    db, manager = fresh_manager(client)
    document, candidates = knowledge_candidates(db, manager)
    identifier, visible = saved(manager, "search_knowledge_base", {"evidence": candidates})
    assert 0 < len(visible["evidence"]) < len(candidates)
    actual = json.loads(manager.messages[-1]["content"])
    assert actual["total_candidates"] == len(candidates)
    assert actual["next_candidate_offset"] == visible["next_candidate_offset"]
    offset = visible["next_candidate_offset"]
    page = manager.read_result({"result_id": str(identifier), "candidate_offset": offset})
    assert page["evidence"][0] == candidates[offset]
    page_id, _ = saved(manager, "read_tool_result", page, "page")
    if change == "delete":
        document.status = DocumentStatus.DELETED
    elif change == "replace":
        document.is_active = False
    else:
        from app.models import KnowledgeBaseStatus
        db.get(KnowledgeBase, document.knowledge_base_id).status = KnowledgeBaseStatus.ARCHIVED
    db.commit()
    assert manager.read_result({"result_id": str(identifier)})["error_code"] == "result_access"
    assert manager.read_result({"result_id": str(page_id), "field": "text"})["error_code"] == "result_access"


def test_large_mcp_text_is_saved_paged_and_revocation_blocks_saved_pages(client):
    from app.mcp_client import tool_digest
    db, manager = fresh_manager(client)
    server = MCPServer(owner_id=manager.access.user_id, account_id=manager.access.user_id,
        name="Remote", url="https://example.test/mcp", transport="sse", tools=[], allowed_tools=["read"],
        enabled=True, authorization_version=1, version=1, header_names=[])
    db.add(server); db.commit()
    alias = f"mcp_{server.id.hex}_{tool_digest({'name': 'read'})[:20]}"
    identifier, visible = saved(manager, alias, {"content": [{"type": "text", "text": "大结果" * 200000 + "尾部"}],
        "isError": False, "_extension": {"tool": alias, "server_id": str(server.id), "version": 1}})
    page = manager.read_result({"result_id": str(identifier), "field": "content.0.text", "find": "尾部"})
    assert page["text"] == "尾部" and page["offset"] == 600000
    page_id, _ = saved(manager, "read_tool_result", page, "page")
    server.allowed_tools = []; db.commit()
    assert manager.read_result({"result_id": str(identifier)})["error_code"] == "result_access"
    assert manager.read_result({"result_id": str(page_id)})["error_code"] == "result_access"


def test_candidate_continuation_enters_citations_only_when_delivered_to_model(client):
    db, manager = fresh_manager(client)
    document, candidates = knowledge_candidates(db, manager)
    identifier, visible = saved(manager, "search_knowledge_base", {"evidence": candidates})
    offset = visible["next_candidate_offset"]
    class Model:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [ToolCall("continue", "read_tool_result",
                    {"result_id": str(identifier), "candidate_offset": offset})])
            page = json.loads(messages[-1]["content"])
            assert page["evidence"][0]["chunk_id"] == candidates[offset]["chunk_id"]
            return ToolResponse("已读取候选 [" + candidates[offset]["chunk_id"] + "]", [])
    run = run_retrieval_agent(query="续查", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager)
    assert run.citations and run.citations[0]["chunk_id"] == candidates[offset]["chunk_id"]
    assert candidates[0]["chunk_id"] not in {item["chunk_id"] for item in run.citations}


def test_mcp_evidence_fields_cannot_be_promoted_to_knowledge_citations(client):
    from app.mcp_client import tool_digest
    db, manager = fresh_manager(client)
    _, candidates = knowledge_candidates(db, manager)
    server = MCPServer(owner_id=manager.access.user_id, account_id=manager.access.user_id,
        name="Remote", url="https://example.test/mcp", transport="sse", tools=[], allowed_tools=["read"],
        enabled=True, authorization_version=1, version=1, header_names=[])
    db.add(server); db.commit()
    alias = f"mcp_{server.id.hex}_{tool_digest({'name': 'read'})[:20]}"
    identifier, _ = saved(manager, alias, {"evidence": [{**candidates[0], "text": "remote replacement"}],
        "_extension": {"server_id": str(server.id), "version": 1}})
    assert manager.read_result({"result_id": str(identifier), "candidate_offset": 0}).get("error")
    class Model:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [ToolCall("read-remote", "read_tool_result",
                    {"result_id": str(identifier), "field": "evidence.0.text"})])
            data = json.loads(messages[-1]["content"])
            assert data["text"] == "remote replacement" and "evidence" not in data
            return ToolResponse("仅作为外部观察", [])
    run = run_retrieval_agent(query="查阅", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager)
    assert run.citations == []


def test_discovery_pages_library_and_document_lists_independently(client):
    db, manager = fresh_manager(client)
    document, _ = knowledge_candidates(db, manager)
    libraries = [KnowledgeBase(account_id=manager.access.user_id, owner_id=manager.access.user_id,
                              name=f"directory-{index:02d}")
        for index in range(35)]
    db.add_all(libraries)
    for index in range(35):
        db.add(Document(knowledge_base_id=document.knowledge_base_id, filename=f"page-{index:02d}.txt",
            content_type="text/plain", size_bytes=1, object_key=str(uuid4()), content_hash="a" * 64,
            status=DocumentStatus.READY))
    db.commit()
    seen_libraries, seen_documents = [], []
    class Model:
        step = 0
        def complete_with_tools(self, messages, tools, **kwargs):
            self.step += 1
            if self.step == 1:
                return ToolResponse(None, [ToolCall("catalog-1", "discover_knowledge_bases", {"limit": 20})])
            page = json.loads(messages[-1]["content"])
            seen_libraries.extend(item["id"] for item in page["knowledge_bases"])
            seen_documents.extend(item["id"] for item in page["documents"])
            if self.step == 2:
                assert page["next_library_offset"] == page["next_document_offset"] == 20
                return ToolResponse(None, [ToolCall("catalog-2", "discover_knowledge_bases",
                    {"limit": 20, "library_offset": 20, "document_offset": 20})])
            assert page["next_library_offset"] is None and page["next_document_offset"] is None
            return ToolResponse("目录读取完成", [])
    run = run_retrieval_agent(query="目录", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager)
    assert run.answer == "目录读取完成"
    assert len(seen_libraries) == len(set(seen_libraries)) == 36
    assert len(seen_documents) == len(set(seen_documents)) == 36


def test_oversized_single_candidate_text_is_readable_and_stale_read_is_rejected(client):
    db, manager = fresh_manager(client)
    document, candidates = knowledge_candidates(db, manager)
    candidates[0]["text"] = "中文😀" * 20000 + "唯一尾部"
    identifier, page = saved(manager, "search_knowledge_base", {"evidence": [candidates[0]]})
    assert not page["evidence"] and page["text_fields"] == ["evidence.0.text"]
    tail = manager.read_result({"result_id": str(identifier), "field": "evidence.0.text", "find": "唯一尾部"})
    assert tail["text"] == tail["evidence"][0]["text"] == "唯一尾部"
    assert tail["offset"] == 60000
    assert manager.read_result({"result_id": str(identifier), "field": "credentials"}).get("error")
    missing = manager.read_result({"result_id": str(identifier), "field": "evidence.0.text", "find": "不存在"})
    assert missing["found"] is False and "text" not in missing


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_agent_discovers_loads_and_runs_over_20_fresh_mcp_observations(client, monkeypatch, protocol):
    from datetime import datetime, timezone
    from app import mcp_client
    from app.extensions import ExtensionRuntime
    from app.models import ChatMessage
    db, manager = fresh_manager(client)
    manager.config = replace(manager.config, api_protocol=protocol)
    definition = {"name": "observe", "description": "observe current value", "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
    server = MCPServer(owner_id=manager.access.user_id, account_id=manager.access.user_id,
        name="Remote", url="https://example.test/mcp", transport="sse", tools=[definition],
        allowed_tools=["observe"], enabled=True, authorization_version=1, version=1,
        last_checked_at=datetime.now(timezone.utc), header_names=[])
    message = ChatMessage(conversation_id=manager.conversation_id, sequence=5, role="assistant",
        request_id=manager.request_id, content="")
    db.add_all([server, message]); db.commit()
    runtime = ExtensionRuntime(manager.factory, manager.access, server_ids=[server.id],
        message_id=message.id, request_id=manager.request_id, provider_config=manager.config)
    calls = []
    def remote(*args):
        calls.append(args)
        return {"value": len(calls)}
    monkeypatch.setattr(mcp_client, "call", remote)
    class Model:
        def complete_with_tools(self, messages, tools, **kwargs):
            results = [item for item in messages if item.get("role") == "tool"]
            if not results:
                return ToolResponse(None, [ToolCall("discover", "search_mcp_tools", {"query": "observe"})])
            data = json.loads(results[-1]["content"])
            if results[-1]["name"] == "search_mcp_tools":
                return ToolResponse(None, [ToolCall("load", "load_mcp_tool", {"alias": data["tools"][0]["alias"]})])
            alias = next(tool["function"]["name"] for tool in tools if tool["function"]["name"].startswith("mcp_"))
            if results[-1]["name"] == alias:
                assert data["value"] == len(calls)
                if len(calls) == 25:
                    return ToolResponse("25 次当前观察完成", [])
            return ToolResponse(None, [ToolCall("same-provider-id", alias, {})])
    run = run_retrieval_agent(query="重复观察", knowledge_base_ids=[], history=[], factory=manager.factory,
        access=manager.access, retrieve=None, provider=Model(), context_manager=manager, extensions=runtime)
    assert run.answer == "25 次当前观察完成" and len(calls) == 25
