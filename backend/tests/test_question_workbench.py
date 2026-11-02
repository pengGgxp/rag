from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from app import auth, question_workbench as qw
from app.extensions import ExtensionRuntime
from app.models import AuthenticationFlow, LoginSession, Membership, QuestionWorkbenchGrant, User, Workspace
from test_auth import register


@pytest.fixture
def bridge(accounts, monkeypatch):
    config = qw.get_settings()
    monkeypatch.setattr(config, "question_workbench_url", "http://localhost:3200")
    monkeypatch.setattr(config, "question_workbench_api_url", "http://localhost:8100")
    monkeypatch.setattr(config, "question_workbench_secret", SecretStr("test-qw-connection-secret-" * 3))
    return accounts


def test_login_code_single_use_service_auth_and_revocation(bridge):
    (owner, _, anonymous), db = bridge
    register(owner)
    assert anonymous.post("/question-workbench/authorize", json={"state": "s" * 40}).status_code == 401
    result = owner.post("/question-workbench/authorize", json={"state": "s" * 40})
    assert result.status_code == 200
    query = parse_qs(urlsplit(result.json()["redirect"]).query)
    assert query["state"] == ["s" * 40]
    code = query["code"][0]
    assert anonymous.post("/question-workbench/exchange", json={"code": code}).status_code == 401
    headers = {"Authorization": "Bearer " + qw.get_settings().question_workbench_secret.get_secret_value(), "X-RAG-Request": "", "Origin": ""}
    result = anonymous.post("/question-workbench/exchange", json={"code": code}, headers=headers)
    assert result.status_code == 200
    who = result.json()
    assert who["user_id"] and "session_hash" in who
    assert anonymous.post("/question-workbench/exchange", json={"code": code}, headers=headers).status_code == 401
    assert anonymous.post("/question-workbench/introspect", json={"identity": who}, headers=headers).status_code == 200
    assert anonymous.post("/question-workbench/introspect", json={"identity": {**who, "user_id": str(uuid4())}}, headers=headers).status_code == 403
    owner.post("/auth/logout")
    assert anonymous.post("/question-workbench/introspect", json={"identity": who}, headers=headers).status_code == 403


def test_expired_code_and_fixed_callback(bridge):
    (owner, _, anonymous), db = bridge
    register(owner)
    result = owner.post("/question-workbench/authorize", json={"state": "s" * 40, "redirect_uri": "https://evil.example"})
    assert result.status_code == 422
    result = owner.post("/question-workbench/authorize", json={"state": "s" * 40})
    code = parse_qs(urlsplit(result.json()["redirect"]).query)["code"][0]
    db.get(AuthenticationFlow, auth.digest(code)).expires_at = auth.now() - timedelta(seconds=1)
    db.commit()
    headers = {"Authorization": "Bearer " + qw.get_settings().question_workbench_secret.get_secret_value()}
    assert anonymous.post("/question-workbench/exchange", json={"code": code}, headers=headers).status_code == 401


def test_explicit_grant_selection_version_and_runtime_revoke(bridge):
    (owner, _, _), db = bridge
    session = register(owner)
    assert owner.get("/question-workbench").json()["enabled"] is False
    assert owner.post("/conversations", json={"question_workbench": True}).status_code == 403
    assert owner.put("/question-workbench/grant", json={"enabled": True, "version": 0}).status_code == 422
    grant = owner.put("/question-workbench/grant", json={"enabled": True, "version": 0, "acknowledge": True})
    assert grant.status_code == 200
    assert owner.put("/question-workbench/grant", json={"enabled": True, "version": 0, "acknowledge": True}).status_code == 409
    conversation = owner.post("/conversations", json={"question_workbench": True})
    assert conversation.status_code == 201, conversation.text
    assert conversation.json()["question_workbench"] is True
    access = auth.Access(UUID(session["user"]["id"]), UUID(session["workspace_id"]), "admin", auth.digest(owner.cookies.get(auth.COOKIE)))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    without = ExtensionRuntime(factory, access)
    assert "save_questions" not in [t["function"]["name"] for t in without.tools]
    runtime = ExtensionRuntime(factory, access, question_workbench=True)
    assert "save_questions" in [t["function"]["name"] for t in runtime.tools]
    assert runtime.execute("load_skill", {"skill_id": "question-workbench"})["instructions"]
    assert owner.put("/question-workbench/grant", json={"enabled": False, "version": 1}).status_code == 200
    assert "error" in runtime.execute("save_questions", {"questions": []})


def test_tools_scope_no_owner_parameters_and_safe_timeout(bridge, monkeypatch):
    (owner, _, _), db = bridge
    session = register(owner)
    owner.put("/question-workbench/grant", json={"enabled": True, "version": 0, "acknowledge": True})
    access = auth.Access(UUID(session["user"]["id"]), UUID(session["workspace_id"]), "admin", auth.digest(owner.cookies.get(auth.COOKIE)))
    runtime = ExtensionRuntime(sessionmaker(bind=db.get_bind(), expire_on_commit=False), access, question_workbench=True)
    assert runtime.execute("save_questions", {"user_id": str(uuid4()), "questions": []}).get("error")
    assert runtime.execute("read_question", {"id": "../../private"}).get("error")
    sent = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def request(self, *args, **kwargs):
            sent.append(kwargs)
            raise httpx.ReadTimeout("timeout")
        def get(self, *args, **kwargs):
            raise httpx.ReadTimeout("result unavailable")
    monkeypatch.setattr(qw.httpx, "Client", Client)
    args = {"questions": [{"kind": "short_answer", "stem": "题干", "answer": ["答案"]}]}
    assert runtime.execute("save_questions", args)["uncertain"] is True
    assert runtime.execute("save_questions", args)["uncertain"] is True
    assert len(sent) == 1
    assert "user_id" not in sent[0]["json"]
    assert "Authorization" in sent[0]["headers"]


def test_lost_creation_response_is_reconciled_without_another_write(bridge, monkeypatch):
    (owner, _, _), db = bridge
    session = register(owner)
    owner.put("/question-workbench/grant", json={"enabled": True, "version": 0, "acknowledge": True})
    access = auth.Access(UUID(session["user"]["id"]), UUID(session["workspace_id"]), "admin", auth.digest(owner.cookies.get(auth.COOKIE)))
    runtime = ExtensionRuntime(sessionmaker(bind=db.get_bind(), expire_on_commit=False), access, question_workbench=True)
    writes, reads = [], []
    identifier = str(uuid4())

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def request(self, *args, **kwargs):
            writes.append(kwargs)
            raise httpx.ReadTimeout("response lost after commit")
        def get(self, url, **kwargs):
            reads.append((url, kwargs))
            return httpx.Response(200, json={"items": [{"id": identifier}]})

    monkeypatch.setattr(qw.httpx, "Client", Client)
    result = runtime.execute("save_questions", {"questions": [{"kind": "short_answer", "stem": "题干", "answer": ["答案"]}]})
    assert result["items"][0]["id"] == identifier
    assert result["items"][0]["url"].endswith("/questions/" + identifier)
    assert len(writes) == len(reads) == 1
    assert reads[0][1]["params"] == {"operation": "questions"}
    assert reads[0][0].endswith("/operations/" + writes[0]["headers"]["Idempotency-Key"])
