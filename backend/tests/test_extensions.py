"""Platform text methods replace personal bundles; private data is never promoted."""
import json
from contextlib import nullcontext
from uuid import UUID, uuid4

import pytest
from httpx_sse import EventSource
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.identity import auth
from app.conversations import execution as conversations
from app.agent.conversation_context import scope_digest
from app.integrations.runtime import ExtensionRuntime
from app.model_runtime.client import LLMUsage, ToolCall, ToolResponse
from app.models import AgentSkill, AgentSkillFile, Conversation, LoginSession, MCPServer, SiteAuditEvent, SiteSkill
from auth_helpers import StreamingTestProvider
from test_auth import register
from test_shared_providers import setup_site, provider
from test_request_models import send


def access_for(client, db):
    session = db.get(LoginSession, auth.digest(client.cookies.get("rag_session")))
    return auth.get_access(session)


def test_non_tool_model_ignores_platform_and_historical_extensions(accounts, monkeypatch):
    from app.models import SiteModelProvider
    from test_thinking import effort
    admin, db, _ = setup_site(accounts, monkeypatch)
    model = provider(admin)
    row = db.get(SiteModelProvider, UUID(model["id"]))
    row.thinking_config = {**effort("openai"), "supports_tools": False}
    row.thinking_version = 1
    chat = admin.post("/conversations", json={}).json()
    conversation = db.get(Conversation, UUID(chat["id"]))
    conversation.skill_ids, conversation.mcp_server_ids = [str(uuid4())], [str(uuid4())]
    db.commit()

    class Model(StreamingTestProvider):
        def complete_with_tools(self, messages, tools, tool_choice):
            assert not tools and "整理报告时使用" not in str(messages)
            self.last_usage = LLMUsage(prompt_tokens=10, completion_tokens=1)
            return ToolResponse("普通回答", [], self.last_usage)

    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Model)
    events, _ = send(admin, chat["id"], model["id"])
    assert events[-1].json()["status"] == "completed", events[-1].json().get("error_detail")
    assert events[-1].json()["tool_events"] == []


def test_retired_or_stale_skill_protocol_content_does_not_reenter_requests(monkeypatch):
    from types import SimpleNamespace
    from app.integrations import mcp_client
    from app.model_catalog.thinking import history_message
    config = SimpleNamespace(continuation_required=True, site_provider_id="provider", model="model",
                             thinking=SimpleNamespace(adapter="deepseek"), api_protocol="chat_completions", connection_id="connection")
    row = SimpleNamespace(role="assistant", content="Historical answer", encrypted_continuation="opaque")
    messages = [{"role": "assistant", "tool_calls": [{"id": "load", "function": {"name": "load_skill"}}]},
                {"role": "tool", "tool_call_id": "load", "content": json.dumps({"skill_id": "old-private", "instructions": "Private instructions"})}]
    private = {"provider": "provider", "model": "model", "adapter": "deepseek", "messages": messages}
    monkeypatch.setattr(mcp_client, "decrypt_headers", lambda _: {"continuation": json.dumps(private)})
    plain = [{"role": "assistant", "content": "Historical answer"}]
    assert history_message(row, config, []) == plain
    messages[-1]["content"] = json.dumps({"id": "platform", "version": 1, "instructions": "Approved method"})
    assert history_message(row, config, [("platform", 1)]) == messages
    assert history_message(row, config, [("platform", 2)]) == plain
    assert history_message(row, config, []) == plain
    messages[0]["tool_calls"][0]["function"]["name"] = "read_skill_resource"
    assert history_message(row, config, [("platform", 1)]) == messages
    assert history_message(row, config, [("platform", 2)]) == plain
    messages[0]["tool_calls"][0]["function"]["name"] = "mcp_old_tool"
    assert history_message(row, config, [("platform", 1)]) == plain
    messages[-1]["content"] = json.dumps({"content": "Private external result",
        "_extension": {"tool": "mcp_old_tool", "version": 3}})
    assert history_message(row, config, [("mcp_old_tool", 3)]) == messages
    assert history_message(row, config, [("mcp_old_tool", 4)]) == plain
    assert history_message(row, config, []) == plain
