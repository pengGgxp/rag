import secrets
from datetime import timedelta

from app.auth import COOKIE, digest, now
from app.models import LoginSession, Membership, User, Workspace, LEGACY_WORKSPACE_ID
from app.llm import OpenAICompatibleChatProvider


class StreamingTestProvider(OpenAICompatibleChatProvider):
    """Adapt deterministic workflow stubs to the same streaming entry point as production."""
    def stream_with_tools(self, messages, tools, *, tool_choice="auto"):
        result = self.complete_with_tools(messages, tools, tool_choice=tool_choice)
        if result.content:
            yield "delta", {"text": result.content}
        yield "complete", result


def connection_id(db, *, url="https://example.test/v1", secret="test-only"):
    from app.models import ModelConnection
    row = ModelConnection(name="Test", platform="custom", base_url=url, encrypted_api_key=secret)
    db.add(row)
    db.flush()
    return row.id


def configured_model(db, workspace_id, monkeypatch=None):
    """Seed persisted configuration for workflow tests; never bypass provider resolution."""
    from app.models import SiteModelProvider, ModelConnection, LLMProviderConfig, UsagePolicy
    from app import llm_providers
    from app.credits import DEFAULT_USAGE_POLICIES
    if monkeypatch:
        monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda _value: "test-only")
    prices = {key: {"capability": key, "unit": "tokens", "unit_size": 1000,
        "credits_per_unit": 1, "enabled": True} for key in (
            "llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens")}
    connection = ModelConnection(name="Test", base_url="https://example.test/v1", encrypted_api_key="test-only")
    db.add(connection); db.flush()
    provider = SiteModelProvider(key=f"test-{workspace_id}", name="Test", model="test-model",
        connection_id=connection.id, enabled=True, prices=prices)
    db.add(provider); db.flush()
    binding = LLMProviderConfig(workspace_id=workspace_id, site_provider_id=provider.id,
        key="test", name="Test", model="test-model", base_url="", encrypted_api_key="", active=True)
    db.add(binding)
    db.flush()
    for policy in DEFAULT_USAGE_POLICIES:
        db.add(UsagePolicy(workspace_id=workspace_id, **policy))
        db.add(UsagePolicy(workspace_id=workspace_id, provider_id=binding.id, **policy))
    db.add(UsagePolicy(workspace_id=workspace_id, provider_id=binding.id, capability="llm_cached_input_tokens",
        unit="tokens", unit_size=1000, credits_per_unit=0))
    db.commit()
    return binding


def model_id(http):
    return next(item["id"] for item in http.get("/models/available").json()["providers"] if item["available"])


def authenticate(http, db):
    """Seed a real server-side session; do not bypass authorization dependencies."""
    db.add(Workspace(id=LEGACY_WORKSPACE_ID, name="test workspace"))
    user = User(email="owner@example.com", name="Owner")
    db.add(user)
    db.flush()
    db.add(Membership(workspace_id=LEGACY_WORKSPACE_ID, user_id=user.id, role="admin"))
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=digest(raw), user_id=user.id, workspace_id=LEGACY_WORKSPACE_ID,
                        csrf_token=csrf, expires_at=now()+timedelta(hours=1)))
    db.commit()
    http.cookies.set(COOKIE, raw)
    http.headers.update({"X-RAG-Request": "1", "X-CSRF-Token": csrf})
    return user
