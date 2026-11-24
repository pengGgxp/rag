import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import anyio
import pytest
from mcp.types import Tool

from app.integrations import mcp_client
from app.agent.conversation_context import estimate_input
from app.integrations.runtime import ExtensionRuntime
from app.model_runtime.client import ChatProviderConfig
from app.models import MCPServer
from remote_receipt_helpers import answer_identity
from test_extensions import access_for
from test_personal_extensions import connection, update_connection
from test_shared_providers import setup_site


def tool(index, padding=0):
    return {"name": f"observe_{index:04d}", "description": "authorized observation",
        "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object",
            "properties": {"query": {"type": "string", "description": "x" * padding}},
            "additionalProperties": False}}


@pytest.fixture
def runtime(accounts, monkeypatch):
    http, db, _ = setup_site(accounts, monkeypatch)
    row = connection(http)
    definitions = [tool(index, 3000) for index in range(150)]
    monkeypatch.setattr(mcp_client, "discover", lambda *_: definitions)
    row = http.post(f"/agent/mcp/{row['id']}/sync", json={"version": row["version"]}).json()
    row = update_connection(http, row, enabled=True, allowed_tools=[item["name"] for item in definitions],
        acknowledge_external_access=True)
    assert row.status_code == 200, row.text
    message, request = answer_identity(http, db)
    def build(config=None):
        return ExtensionRuntime(lambda: nullcontext(db), access_for(http, db), [],
            [row.json()["id"]], message, request, provider_config=config)
    return build, db, UUID(row.json()["id"])


def test_metadata_pages_do_not_load_or_execute_and_revoke_is_immediate(runtime, monkeypatch):
    build, db, server_id = runtime
    engine = build()
    monkeypatch.setattr(mcp_client, "call", lambda *_: pytest.fail("discovery executed a remote tool"))
    offered = engine.model_tools([])
    assert not any(item["function"]["name"].startswith("mcp_") for item in offered)
    assert "search_mcp_tools" in {item["function"]["name"] for item in offered}
    seen, offset = [], 0
    while True:
        page = engine.execute("search_mcp_tools", {"query": "observe", "offset": offset, "limit": 50})
        assert "inputSchema" not in str(page)
        seen.extend(item["alias"] for item in page["tools"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 150
    assert engine.execute(seen[0], {})["error_code"] == "mcp_tool_not_loaded"
    assert engine.execute("load_mcp_tool", {"alias": seen[0]})["available"] == "next_model_step"
    assert engine.execute(seen[0], {})["error_code"] == "mcp_tool_not_loaded"
    engine.model_tools([])
    server = db.get(MCPServer, server_id)
    server.allowed_tools = []; server.authorization_version += 1; db.commit()
    assert engine.execute("search_mcp_tools", {})["tools"] == []
    assert engine.execute("load_mcp_tool", {"alias": seen[0]})["error_code"] == "mcp_discovery_access"
    assert engine.execute(seen[0], {}).get("error")


def test_budget_eviction_reconstruction_and_unmodified_large_schema_rejection(runtime, monkeypatch):
    build, db, server_id = runtime
    engine = build()
    aliases = list(engine.bindings)
    loaded_messages = []
    for alias in aliases[:10]:
        loaded = engine.execute("load_mcp_tool", {"alias": alias})
        assert loaded["loaded_alias"] == alias
        loaded_messages.append({"role": "tool", "name": "load_mcp_tool", "content": json.dumps(loaded)})
    assert loaded["evicted_aliases"] and aliases[0] not in engine.mcp_catalog.loaded
    assert estimate_input([], list(engine.mcp_catalog.loaded.values())) <= 6000
    restored = build()
    restored.model_tools(loaded_messages)
    assert list(restored.mcp_catalog.loaded) == list(engine.mcp_catalog.loaded)
    # An effective compacted window lacking load receipts simply starts with discovery.
    assert not build().model_tools([])[-1]["function"]["name"].startswith("mcp_")
    restored.provider_config = ChatProviderConfig("", "", "small", context_capacity=1000, max_output_tokens=500)
    previous = json.loads(json.dumps(restored.bindings[aliases[0]]["tool"]))
    result = restored.execute("load_mcp_tool", {"alias": aliases[0]})
    assert result["error_code"] == "mcp_definition_capacity"
    assert restored.bindings[aliases[0]]["tool"] == previous
    offered = restored.model_tools([])
    assert not any(item["function"]["name"].startswith("mcp_") for item in offered)
    assert restored.execute(aliases[0], {})["error_code"] == "mcp_tool_not_loaded"


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic", "gemini"])
def test_dynamic_schema_is_valid_for_each_protocol_and_not_in_catalog_estimate(runtime, protocol):
    from app.model_runtime.client import OpenAICompatibleChatProvider
    build, _, _ = runtime
    config = ChatProviderConfig("https://example.test", "unused", "model", api_protocol=protocol,
        context_capacity=32000, max_output_tokens=4000)
    engine = build(config)
    initial = engine.model_tools([])
    cost_before = estimate_input([], initial)
    alias = next(iter(engine.bindings))
    engine.execute("load_mcp_tool", {"alias": alias})
    actual = engine.model_tools([])
    assert estimate_input([], actual) - cost_before < 2000
    assert engine.mcp_catalog.cost(list(engine.mcp_catalog.loaded.values())) <= engine.mcp_catalog.budget
    payload = OpenAICompatibleChatProvider(config=config)._payload([{"role": "user", "content": "go"}], tools=actual, stream=True)
    assert alias in json.dumps(payload)
    assert list(engine.bindings)[-1] not in json.dumps(payload)


class Pages:
    def __init__(self, pages):
        self.pages = iter(pages)
    async def list_tools(self, cursor=None):
        return next(self.pages)


def page(tools=(), cursor=None):
    return SimpleNamespace(tools=[Tool(**item) for item in tools], nextCursor=cursor)


def test_catalog_accepts_more_than_100_tools_with_cursor_and_utf8_capacity():
    definitions = [tool(index) for index in range(2048)]
    client = Pages([page(definitions[:1000], "next"), page(definitions[1000:])])
    result = anyio.run(mcp_client._tools, client, {})
    assert len(result) == 2048 and result[-1]["name"] == "observe_2047"


@pytest.mark.parametrize("pages,reason", [
    ([page([tool(1)], "repeat"), page([], "repeat")], "重复分页"),
    ([page([tool(1), tool(1)])], "名称重复"),
    ([page([], "中" * 22000)], "游标"),
    ([page([], str(index)) for index in range(101)], "100 页"),
    ([page([tool(index) for index in range(2049)])], "过多"),
    ([page([{**tool(0), "description": "中" * (3 * 1024 * 1024)}])], "定义过大"),
])
def test_catalog_rejects_cycles_duplicates_page_cursor_item_and_utf8_budgets(pages, reason):
    with pytest.raises(mcp_client.MCPError, match=reason):
        anyio.run(mcp_client._tools, Pages(pages), {})
