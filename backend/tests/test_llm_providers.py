from uuid import UUID, uuid4

from app import llm_providers
from app.models import LLMProviderConfig, UsageEvent, UsagePolicy
from app.credits import authorization_hold, settle_model_usage
from test_auth import accounts, register


def test_admin_can_create_and_switch_chat_model_providers(accounts, monkeypatch):
    (owner, viewer, _), db = accounts
    register(owner)
    monkeypatch.setattr(llm_providers, "_encrypt_api_key", lambda value: f"encrypted:{value}")
    monkeypatch.setattr(llm_providers, "_decrypt_api_key", lambda value: value.removeprefix("encrypted:"))

    first = owner.post("/settings/llm-providers", json={
        "key": "qwen", "name": "通义千问", "base_url": "https://dash.example/v1",
        "model": "qwen-plus", "api_key": "secret-1",
    })
    assert first.status_code == 201, first.text
    first_data = first.json()
    assert first_data["active"] is True and first_data["has_api_key"] is True

    second = owner.post("/settings/llm-providers", json={
        "key": "deepseek", "name": "DeepSeek", "base_url": "https://deep.example/v1",
        "model": "deepseek-chat", "api_key": "secret-2",
    })
    assert second.status_code == 201
    switched = owner.post(f"/settings/llm-providers/{second.json()['id']}/activate")
    assert switched.status_code == 200 and switched.json()["active"] is True
    listed = owner.get("/settings/llm-providers").json()
    assert {item["id"]: item["active"] for item in listed} == {first_data["id"]: False, second.json()["id"]: True}
    assert "secret-1" not in owner.get("/settings/llm-providers").text



def test_provider_pricing_is_isolated_and_snapshotted(accounts, monkeypatch):
    (owner, _, _), db = accounts
    session = register(owner)
    workspace_id = UUID(session["workspace_id"])
    user_id = UUID(session["user"]["id"])
    provider = LLMProviderConfig(
        workspace_id=workspace_id, key="qwen", name="Qwen",
        base_url="https://dash.example/v1", model="qwen-plus",
        encrypted_api_key="encrypted:key", active=True,
    )
    db.add(provider)
    db.flush()
    db.add(UsagePolicy(
        workspace_id=workspace_id, provider_id=provider.id,
        capability="llm_input_tokens", unit="tokens", unit_size=1000,
        credits_per_unit=7, enabled=True,
    ))
    db.add(UsagePolicy(
        workspace_id=workspace_id, provider_id=provider.id,
        capability="llm_output_tokens", unit="tokens", unit_size=1000,
        credits_per_unit=9, enabled=True,
    ))
    db.commit()
    request_id = uuid4()
    authorization_hold(db, workspace_id, user_id, request_id)
    settle_model_usage(db, workspace_id, user_id, request_id, [{
        "prompt_tokens": 1000, "completion_tokens": 0,
        "provider_id": str(provider.id), "provider_key": "qwen",
    }])
    db.commit()
    event = db.query(UsageEvent).filter(
        UsageEvent.request_id == request_id,
        UsageEvent.capability == "llm_input_tokens",
    ).one()
    assert event.credits == 7
    assert event.provider_key == "qwen"
    assert event.policy_snapshot["credits_per_unit"] == 7
