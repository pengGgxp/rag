from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import auth, model_billing, search_billing, search_settings, web_search
from app.models import UserCreditAccount, CreditLedgerEntry, ModelBillingSettings, ModelUsageRequest, SearchToolCall, SiteSearchSettings, UsageEvent, UserModelPolicy
from test_model_billing import metered, call
from test_web_search import configuration, search_admin


def setup_search(client, db):
    from app.models import User
    session = client.get("/auth/me").json()
    db.get(User, UUID(session["user"]["id"])).is_site_admin = True
    db.commit()
    saved = client.put("/site/admin/search", json=configuration())
    assert saved.status_code == 200, saved.text
    return search_settings.runtime_config(db)


def access(client, workspace, user):
    return auth.Access(user, auth.digest(client.cookies.get(auth.COOKIE)))


@pytest.fixture
def paid(search_admin, monkeypatch):
    owner, db, session = search_admin
    workspace, user = UUID(session["user"]["id"]), UUID(session["user"]["id"])
    db.add(ModelBillingSettings(id=1, enabled=True, shared_pricing_enabled=True))
    db.add(UserModelPolicy(user_id=workspace, billing_multiplier_percent=125))
    db.commit()
    config = setup_search(owner, db)
    request = uuid4()
    model_billing.start_request(db, workspace, user, request, None, 0, search_config=config, tool_only=True)
    db.commit()
    runtime = search_billing.SearchRuntime(sessionmaker(bind=db.get_bind()), access(owner, workspace, user), request, config)
    return owner, db, workspace, user, request, runtime


def test_tool_prices_multiplier_snapshot_and_replay(paid, monkeypatch):
    _, db, workspace, _, request, runtime = paid
    calls = []
    monkeypatch.setattr(web_search, "invoke", lambda config, capability, args: (
        calls.append(capability) or {"effective": True, "result_count": 1, "results": []}))
    settings = db.get(SiteSearchSettings, 1)
    settings.prices = {key: {**price, "credits_per_unit": 9999} for key, price in settings.prices.items()}
    settings.version += 1
    db.get(UserModelPolicy, workspace).billing_multiplier_percent = 10000
    db.commit()
    first = runtime.execute("web_search", {"query": "public"}, "1", lambda: None)
    assert runtime.execute("web_search", {"query": "public"}, "1", lambda: None) == first
    runtime.execute("web_fetch", {"url": "https://example.com"}, "2", lambda: None)
    assert calls == ["web_search", "web_fetch"]
    assert model_billing.finalize_request(db, request, "cancelled") == Decimal("0.0125")
    db.commit()
    assert model_billing.finalize_request(db, request, "cancelled") == Decimal("0.0125")
    rows = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
    assert len(rows) == 2 and sum(row.credits for row in rows) == Decimal("0.0125")
    assert all(row.units == 1 and row.policy_snapshot["multiplier_percent"] == 125 for row in rows)
    assert db.get(UserCreditAccount, workspace).balance == Decimal("999.9875")
    assert db.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
        CreditLedgerEntry.account_id == workspace, CreditLedgerEntry.kind == "usage_settlement")) == 1
    assert "public" not in str([row.metadata_json for row in rows])


@pytest.mark.parametrize("result", [{"effective": False, "result_count": 0}, web_search.SearchError("搜索失败")])
def test_empty_and_failed_calls_never_charge(paid, monkeypatch, result):
    _, db, workspace, _, request, runtime = paid
    def invoke(*_):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(web_search, "invoke", invoke)
    runtime.execute("web_search", {"query": "empty"}, "1", lambda: None)
    assert model_billing.finalize_request(db, request, "failed") == 0
    db.commit()
    assert db.get(UserCreditAccount, workspace).balance == 1000
    row = db.scalar(select(SearchToolCall).where(SearchToolCall.request_id == request))
    assert row.units == 0 and row.status in ("empty", "failed")


def test_balance_and_disable_prevent_upstream_without_task_call_cap(paid, monkeypatch):
    _, db, workspace, _, request, runtime = paid
    called = []
    monkeypatch.setattr(web_search, "invoke", lambda *_: called.append(True) or {"effective": True})
    db.get(UserCreditAccount, workspace).balance = 0
    db.commit()
    assert "积分不足" in runtime.execute("web_search", {"query": "x"}, "low", lambda: None)["error"]
    assert not called
    db.get(UserCreditAccount, workspace).balance = 1000
    db.commit()
    for i in range(25):
        assert runtime.execute("web_search", {"query": "x"}, str(i), lambda: None)["effective"]
    for i in range(12):
        assert runtime.execute("web_fetch", {"url": "https://example.com"}, f"fetch-{i}", lambda: None)["effective"]
    db.get(SiteSearchSettings, 1).enabled = False
    db.commit()
    assert "暂不可用" in runtime.execute("web_fetch", {"url": "https://example.com"}, "off", lambda: None)["error"]
    assert len(called) == 37


@pytest.mark.parametrize("failure", [False, True])
def test_agent_finishes_after_repeated_empty_or_failed_searches_and_fresh_fetches(paid, monkeypatch, failure):
    import json
    from app.agent.loop import run_retrieval_agent
    from app.model_runtime.client import ToolCall, ToolResponse
    _, db, _, _, request, runtime = paid
    calls = []
    def invoke(config, name, arguments):
        calls.append(name)
        if len(calls) <= 24:
            if failure:
                raise web_search.SearchError("synthetic provider failure")
            return {"effective": False, "result_count": 0, "results": []}
        source = web_search.source("https://example.test/page", "Observation")
        return {"effective": True, "result_count": 1, "results": [source]} if name == "web_search" else {
            **source, "effective": True, "result_count": 1, "content": "current observation"}
    monkeypatch.setattr(web_search, "invoke", invoke)
    class Model:
        def complete_with_tools(self, messages, tools, **kwargs):
            if calls:
                data = json.loads(messages[-1]["content"])
                if len(calls) <= 24:
                    assert data.get("error") if failure else data["result_count"] == 0
                else:
                    assert data["result_count"] == 1
            if len(calls) == 37:
                return ToolResponse("已完成全部观察", [])
            name = "web_search" if len(calls) < 25 else "web_fetch"
            arguments = {"query": "public"} if name == "web_search" else {"url": "https://example.test/page"}
            return ToolResponse(None, [ToolCall("same-provider-id", name, arguments)])
    run = run_retrieval_agent(query="继续查询", knowledge_base_ids=[], history=[], factory=runtime.factory,
        access=runtime.access, retrieve=None, provider=Model(), search=runtime)
    assert run.answer == "已完成全部观察" and calls == ["web_search"] * 25 + ["web_fetch"] * 12
    assert model_billing.finalize_request(db, request, "completed") == Decimal("0.0813")
    db.commit()
    rows = db.scalars(select(SearchToolCall).where(SearchToolCall.request_id == request)).all()
    assert len(rows) == 37 and sum(row.units for row in rows) == 13


def test_started_call_is_not_replayed_and_recovery_retains_confirmed_usage(paid, monkeypatch):
    _, db, _, _, request, runtime = paid
    monkeypatch.setattr(web_search, "invoke", lambda *_: {"effective": True})
    runtime.execute("web_search", {"query": "x"}, "done", lambda: None)
    search_billing.begin(db, request, "web_search", "unfinished")
    db.commit()
    model_billing.recover_requests(db)
    request_row = db.get(ModelUsageRequest, request)
    assert request_row.status == "settled" and request_row.credits == Decimal("0.0063")
    assert db.scalar(select(SearchToolCall).where(SearchToolCall.call_key == "unfinished")).status == "interrupted"


def test_admin_test_tool_only_and_idempotency(search_admin, monkeypatch):
    owner, db, _ = search_admin
    db.add(ModelBillingSettings(id=1, enabled=True, shared_pricing_enabled=True))
    db.commit()
    setup_search(owner, db)
    calls = []
    monkeypatch.setattr(web_search, "invoke", lambda *_: calls.append(True) or {"effective": True, "result_count": 2})
    payload = {"version": 1, "request_id": str(uuid4())}
    response = owner.post("/site/admin/search/test", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["completed"] and response.json()["credits"] == 0.005
    assert owner.post("/site/admin/search/test", json=payload).status_code == 200
    assert len(calls) == 1
    assert owner.post("/site/admin/search/test", json={**payload, "version": 2}).status_code == 409


def test_model_and_tool_exact_amounts_round_only_once(metered, monkeypatch):
    from app.web_search import SearchConfig
    _, db, workspace, user = metered
    prices = {key: {"capability": key, "unit": "calls", "unit_size": 1000,
        "credits_per_unit": .04, "enabled": True} for key in web_search.CAPABILITIES}
    config = SearchConfig(version=1, api_key="test", prices=prices)
    request = uuid4()
    model_billing.start_request(db, workspace, user, request, None, 0, search_config=config)
    db.add(SearchToolCall(request_id=request, call_key="completed", capability="web_search",
        status="completed", units=1, price={**prices["web_search"], "version": 1}))
    db.commit()
    identifier = call(db, request, input=1, output=0)
    from app.models import ModelCallUsage
    model_call = db.get(ModelCallUsage, identifier)
    model_call.prices = {key: {**price, "credits_per_unit": .04} for key, price in model_call.prices.items()}
    db.commit()
    assert model_billing.finalize_request(db, request, "completed") == Decimal("0.0001")
    db.commit()
    rows = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request)).all()
    assert sum(row.credits for row in rows) == Decimal("0.0001")
    assert len(db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "usage_settlement")).all()) == 1


def test_zero_price_retains_usage_when_cancelled(paid, monkeypatch):
    _, db, _, _, request, runtime = paid
    def invoke(*_):
        return {"effective": True, "result_count": 1}
    monkeypatch.setattr(web_search, "invoke", invoke)
    row = db.get(ModelUsageRequest, request)
    row.billing_context = {**row.billing_context, "search": {**row.billing_context["search"], "prices": {
        key: {**price, "credits_per_unit": 0} for key, price in row.billing_context["search"]["prices"].items()}}}
    db.commit()
    runtime.execute("web_search", {"query": "x"}, "free", lambda: None)
    assert model_billing.finalize_request(db, request, "cancelled") == 0
    db.commit()
    event = db.scalar(select(UsageEvent).where(UsageEvent.request_id == request))
    assert event.units == 1 and event.credits == 0


def test_confirmed_tool_usage_stays_pending_until_recharged(paid, monkeypatch):
    from app.credits import recharge
    _, db, workspace, user, request, runtime = paid
    monkeypatch.setattr(web_search, "invoke", lambda *_: {"effective": True, "result_count": 1})
    runtime.execute("web_search", {"query": "public"}, "done", lambda: None)
    db.get(UserCreditAccount, workspace).balance = 0
    db.commit()
    assert model_billing.finalize_request(db, request, "failed") == Decimal("0.0063")
    db.commit()
    assert db.get(ModelUsageRequest, request).status == "pending"
    recharge(db, workspace, user, 1, uuid4(), 1)
    model_billing.settle_pending(db, workspace)
    db.commit()
    assert db.get(UserCreditAccount, workspace).balance == Decimal("0.9937")
    assert model_billing.finalize_request(db, request, "failed") == Decimal("0.0063")
    assert len(db.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "usage_settlement")).all()) == 1
