from fractions import Fraction
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import model_billing as billing
from app.credits import CreditError, ensure_policies, recharge
from app.llm import LLMProviderError, LLMUsage, ToolResponse
from app.models import (CreditAccount, CreditLedgerEntry, ModelBillingSettings, ModelCallUsage,
                        ModelUsageRequest, UsageEvent, UsagePolicy)
from test_auth import register


@pytest.fixture
def metered(accounts):
    (owner, _, _), db = accounts
    session = register(owner)
    workspace, user = UUID(session["workspace_id"]), UUID(session["user"]["id"])
    ensure_policies(db, workspace)
    db.add_all([UsagePolicy(workspace_id=workspace, capability="llm_cached_input_tokens",
        unit="tokens", unit_size=1000, credits_per_unit=0), ModelBillingSettings(id=1, enabled=True)])
    db.commit()
    return owner, db, workspace, user


def provider(usage=None):
    return SimpleNamespace(status=SimpleNamespace(provider_id=None, provider_key=None, model="test"), last_usage=usage)


def start(db, workspace, user):
    identifier = uuid4()
    billing.start_request(db, workspace, user, identifier, None, 1)
    db.commit()
    return identifier


def call(db, request_id, *, input=100, cache=0, output=100):
    identifier = billing.begin_call(db, request_id, provider())
    billing.finish_call(db, request_id, identifier, LLMUsage(prompt_tokens=input+cache,
        completion_tokens=output, cached_input_tokens=cache, cache_status="reported").measured(), "completed")
    db.commit()
    return identifier


def test_multiple_rounds_sum_once_and_allocations_equal_one_debit(metered):
    owner, db, workspace, user = metered
    request_id = start(db, workspace, user)
    for _ in range(3):
        call(db, request_id, input=100, cache=300, output=100)
    assert billing.finalize_request(db, request_id, "completed") == 2  # ceil(3 * .4)
    db.commit()
    assert db.get(CreditAccount, workspace).balance == 998
    events = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
    assert len(events) == 9 and sum(e.credits for e in events) == 2
    assert sum(Fraction(int(e.metadata_json["exact_numerator"]), int(e.metadata_json["exact_denominator"])) for e in events) == Fraction(6,5)
    assert len(db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind=="usage_settlement")).all()) == 1
    assert billing.finalize_request(db, request_id, "completed") == 2
    db.commit()
    assert db.get(CreditAccount, workspace).balance == 998
    assert "policy_snapshot" not in str(owner.get("/usage").json())


def test_missing_cache_price_blocks_before_hold_or_model(metered):
    _, db, workspace, user = metered
    row = db.scalar(select(UsagePolicy).where(UsagePolicy.workspace_id==workspace,
        UsagePolicy.capability=="llm_cached_input_tokens"))
    db.delete(row); db.commit()
    with pytest.raises(HTTPException) as exc:
        start(db, workspace, user)
    assert exc.value.status_code == 503
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ModelUsageRequest)) == 0
    assert db.get(CreditAccount, workspace).balance == 1000


def test_pending_does_not_overdraw_and_next_request_pays_after_recharge(metered):
    owner, db, workspace, user = metered
    request_id = start(db, workspace, user)
    call(db, request_id, input=1_100_000, output=0)
    assert billing.finalize_request(db, request_id, "failed") == 1100
    db.commit()
    assert db.get(CreditAccount, workspace).balance == 1000
    assert owner.get("/usage").json()["pending_credits"] == 1100
    with pytest.raises(CreditError):
        start(db, workspace, user)
    db.rollback()
    recharge(db, workspace, user, 500, uuid4(), 500); db.commit()
    next_request = start(db, workspace, user)
    assert db.get(CreditAccount, workspace).balance == 399
    assert db.get(ModelUsageRequest, request_id).status == "settled"
    billing.finalize_request(db, next_request, "cancelled"); db.commit()
    assert db.get(CreditAccount, workspace).balance == 400
    assert db.get(CreditAccount, workspace).lifetime_spent == 1100
    assert owner.get("/usage").json()["pending_credits"] == 0


def test_snapshots_survive_price_change_and_recovery_keeps_completed_calls(metered):
    _, db, workspace, user = metered
    request_id = start(db, workspace, user)
    first = call(db, request_id, input=1000, output=0)
    row = db.scalar(select(UsagePolicy).where(UsagePolicy.workspace_id==workspace,
        UsagePolicy.capability=="llm_input_tokens"))
    row.credits_per_unit, row.version = 10, 2
    db.commit()
    call(db, request_id, input=1000, output=0)
    unfinished = billing.begin_call(db, request_id, provider()); db.commit()
    billing.recover_requests(db)
    assert db.get(ModelUsageRequest, request_id).credits == 11
    assert db.get(ModelCallUsage, unfinished).status == "interrupted"
    assert db.get(ModelCallUsage, first).prices["llm_input_tokens"]["version"] == 1
    billing.recover_requests(db)
    assert db.get(CreditAccount, workspace).balance == 989


def test_meter_persists_reported_usage_on_failure_but_does_not_estimate(metered):
    _, db, workspace, user = metered
    request_id = start(db, workspace, user)
    meter = billing.ModelMeter(sessionmaker(bind=db.get_bind()), request_id)
    model = provider()
    def failed():
        model.last_usage = LLMUsage(prompt_tokens=400, completion_tokens=20)
        raise LLMProviderError("invalid tool call")
    with pytest.raises(LLMProviderError):
        meter.invoke(model, failed, [])
    def timeout():
        raise LLMProviderError("timeout")
    with pytest.raises(LLMProviderError):
        meter.invoke(model, timeout, [{"content":"x"*5000}])
    billing.finalize_request(db, request_id, "failed"); db.commit()
    calls = db.scalars(select(ModelCallUsage).order_by(ModelCallUsage.round)).all()
    assert calls[0].usage["prompt_tokens"] == 400
    assert calls[1].usage["prompt_tokens"] is None
    assert db.get(ModelUsageRequest, request_id).credits == 1


def test_zero_price_and_maximum_remainder_tie_are_deterministic(metered):
    _, db, workspace, user = metered
    request_id = start(db, workspace, user)
    call(db, request_id, input=100, output=0)
    call(db, request_id, input=100, output=0)
    billing.finalize_request(db, request_id, "cancelled"); db.commit()
    events = db.scalars(select(UsageEvent).where(UsageEvent.capability=="llm_input_tokens")
        .order_by(UsageEvent.idempotency_key)).all()
    assert [e.credits for e in events] == [1,0]
    for price in db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id==workspace)).all():
        price.credits_per_unit = 0
    db.commit()
    identifier = start(db, workspace, user)
    call(db, identifier, input=1000, cache=500, output=1000)
    assert billing.finalize_request(db, identifier, "completed") == 0
    db.commit()
    assert db.get(CreditAccount, workspace).balance == 999


@pytest.mark.parametrize("ending", ["success", "fail", "stop"])
def test_agent_captures_each_round_before_failure_or_stop(metered, monkeypatch, ending):
    from app import conversations
    from app.llm import ToolCall
    from app.models import Conversation
    owner, db, workspace, user = metered
    class Provider:
        status = provider().status
        last_usage = None
        def __init__(self):
            self.round = 0
        def complete_with_tools(self, messages, tools, tool_choice):
            self.round += 1
            if self.round == 1:
                return ToolResponse(None, [ToolCall("a", "load_skill", {})],
                    LLMUsage(prompt_tokens=100, completion_tokens=20))
            assert any(item["role"] == "tool" for item in messages)
            if ending == "fail":
                raise LLMProviderError("timeout")
            self.last_usage = LLMUsage(prompt_tokens=150, completion_tokens=30)
            if ending == "stop":
                with sessionmaker(bind=db.get_bind())() as session:
                    row = session.scalar(select(Conversation))
                    row.active_request_id = None
                    session.commit()
            return ToolResponse("done", [], self.last_usage)
    monkeypatch.setattr(conversations, "OpenAICompatibleChatProvider", Provider)
    created = owner.post("/conversations", json={"knowledge_base_ids":[]}).json()
    request_id = uuid4()
    response = owner.post(f"/conversations/{created['id']}/messages", json={"query":"hello", "request_id":str(request_id)})
    assert response.status_code == 200, response.text
    db.expire_all()
    row = db.get(ModelUsageRequest, request_id)
    assert row.status == "settled" and row.credits == 1
    assert db.scalar(select(func.count()).select_from(ModelCallUsage).where(
        ModelCallUsage.request_id==request_id)) == 2
    assert db.get(CreditAccount, workspace).balance == 999
    duplicate = owner.post(f"/conversations/{created['id']}/messages", json={"query":"hello", "request_id":str(request_id)})
    assert duplicate.status_code in (200,409)
    assert db.get(CreditAccount, workspace).balance == 999
