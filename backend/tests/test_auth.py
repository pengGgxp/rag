import json
import hashlib
import base64
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx_sse import EventSource
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import SecretStr

from app import main

from app.identity import auth
from app.conversations import execution as chat
from app.infra.db import Base
from app.model_runtime.client import ToolResponse
from app.models import (
    AuthToken, Conversation, ExternalIdentity, IngestionJob,
    KnowledgeBase, LoginSession, User, UserCreditAccount,
)
from test_retrieval import add_document

PASSWORD = "long-test-password-123"


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    main.app.dependency_overrides[main.get_db] = lambda: db
    monkeypatch.setattr(auth, "rate_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.knowledge.retrieval.OpenAICompatibleEmbeddingProvider", lambda: SimpleNamespace(
        status=SimpleNamespace(configured=False, provider=None, model=None, dimension=1024),
    ))
    clients = [TestClient(main.app, headers={"X-RAG-Request": "1"}) for _ in range(3)]
    try:
        yield clients, db
    finally:
        for client in clients:
            client.close()
        main.app.dependency_overrides.pop(main.get_db, None)
        db.close()
        engine.dispose()


def register(client, email="owner@example.com", name="Owner"):
    response = client.post("/auth/register", json={"email": email, "name": name, "password": PASSWORD})
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def library(client):
    response = client.post("/knowledge-bases", json={"name": "Product docs"})
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def share(owner, other_email, role="viewer"):
    response = owner.put("/auth/members", json={"email": other_email, "role": role})
    assert response.status_code == 200, response.text


def switch(client, workspace_id):
    response = client.post("/auth/workspace", json={"workspace_id": workspace_id})
    assert response.status_code == 200, response.text


def test_registration_sessions_csrf_logout_and_expiry(accounts):
    (owner, _, anonymous), db = accounts
    assert anonymous.get("/knowledge-bases").status_code == 401
    assert anonymous.get("/conversations").status_code == 401
    result = register(owner)
    cookie = owner.cookies.get(auth.COOKIE)
    assert "httponly" in str(owner.cookies).lower() or cookie
    row = db.get(LoginSession, auth.digest(cookie))
    assert row is not None and row.token_hash != cookie
    assert owner.get("/auth/me").json()["user"]["email"] == "owner@example.com"
    assert owner.post("/knowledge-bases", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert owner.post("/knowledge-bases", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert owner.post("/knowledge-bases", json={"name": "x"}, headers={"X-RAG-Request": ""}).status_code == 403
    library(owner)
    assert owner.post("/auth/logout").status_code == 204
    assert db.get(LoginSession, auth.digest(cookie)) is None
    assert owner.get("/knowledge-bases").status_code == 401
    bad = owner.post("/auth/login", json={"email": "owner@example.com", "password": "wrong"})
    assert bad.status_code == 401
    signed_in = owner.post("/auth/login", json={"email": "OWNER@example.com", "password": PASSWORD})
    assert signed_in.status_code == 200
    assert "HttpOnly" in signed_in.headers["set-cookie"] and "SameSite=lax" in signed_in.headers["set-cookie"]
    row = db.get(LoginSession, auth.digest(owner.cookies.get(auth.COOKIE)))
    row.expires_at = auth.now()-timedelta(seconds=1)
    db.commit()
    assert owner.get("/auth/me").status_code == 401


def test_cannot_revalidate_a_session_for_a_different_user(accounts):
    (owner, other, _), db = accounts
    first = register(owner)
    register(other, "other-admin@example.com")
    private_id = library(owner)
    assert other.get("/knowledge-bases").json() == []
    assert other.get(f"/knowledge-bases/{private_id}/documents").status_code == 404

    other_session = db.get(LoginSession, auth.digest(other.cookies.get(auth.COOKIE)))
    assert "workspace_id" not in LoginSession.__table__.columns
    with pytest.raises(HTTPException) as exc:
        auth.Access(UUID(first["user"]["id"]), other_session.token_hash).revalidate(db)
    assert exc.value.status_code == 403


def test_cross_workspace_id_guesses_do_not_expose_any_resource(accounts):
    (owner, outsider, _), db = accounts
    register(owner)
    register(outsider, "other@example.com", "Other")
    id = library(owner)
    document = add_document(db, db.get(KnowledgeBase, id), "needle private")
    job = IngestionJob(document_id=document.id)
    db.add(job)
    db.commit()
    trace = owner.post(f"/knowledge-bases/{id}/retrieval", json={"query": "needle"}).json()["trace"]
    conv = owner.post("/conversations", json={}).json()["id"]
    assert outsider.get("/knowledge-bases").json() == []
    for path in (
        f"/knowledge-bases/{id}/documents", f"/documents/{document.id}/chunks", f"/jobs/{job.id}",
        f"/traces/{trace['id']}", f"/conversations/{conv}", f"/conversations/{conv}/export",
    ):
        assert outsider.get(path).status_code == 404, path
    for path, payload in (
        (f"/knowledge-bases/{id}/retrieval", {"query": "needle"}),
        (f"/knowledge-bases/{id}/answer", {"query": "needle", "provider_id": str(uuid4())}),
        (f"/conversations/{conv}/messages", {"query": "needle", "provider_id": str(uuid4()), "request_id": str(uuid4())}),
        (f"/conversations/{conv}/stop", {"request_id": str(uuid4())}),
    ):
        assert outsider.post(path, json=payload).status_code == 404, path
    assert outsider.delete(f"/documents/{document.id}").status_code == 404
    assert outsider.delete(f"/conversations/{conv}").status_code == 404
    assert outsider.get("/conversations").json()["items"] == []


def test_workspace_collaboration_routes_are_removed(accounts):
    (owner, other, _), _db = accounts
    register(owner)
    other_identity = register(other, "other@example.com", "Other")
    id = library(owner)
    assert other.post("/auth/workspace", json={"workspace_id": str(uuid4())}).status_code == 404
    assert owner.get("/auth/members").status_code == 404
    for role in ("admin", "editor", "viewer"):
        assert owner.put("/auth/members", json={"email": "other@example.com", "role": role}).status_code == 404
    assert owner.delete(f"/auth/members/{other_identity['user']['id']}").status_code == 404
    assert owner.get(f"/knowledge-bases/{id}/permissions").status_code == 404
    assert owner.put(f"/knowledge-bases/{id}/permissions", json={"restricted": True}).status_code == 404
    assert other.get("/knowledge-bases").json() == []


def test_reset_is_single_use_and_revokes_all_sessions(accounts, monkeypatch):
    (owner, second, _), db = accounts
    register(owner)
    second.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    settings = auth.get_settings().model_copy(update={"smtp_host": "smtp.example.com", "smtp_from": "rag@example.com"})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    sent = []
    from app.identity import site_auth, email_auth
    monkeypatch.setattr(site_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(email_auth, "email_rate_limit", lambda *_a, **_k: None)
    monkeypatch.setattr(auth, "send_reset", lambda email, token, db=None: sent.append((email, token)))
    response = owner.post("/auth/forgot-password", json={"email": "owner@example.com"})
    unknown = owner.post("/auth/forgot-password", json={"email": "unknown@example.com"})
    assert response.json() == unknown.json()
    assert len(sent) == 1 and sent[0][1] not in response.text
    response = second.post("/auth/reset-password", json={"token": sent[0][1], "password": "replacement-password-456"})
    assert response.status_code == 200
    assert owner.get("/knowledge-bases").status_code == 401
    assert second.post("/auth/reset-password", json={"token": sent[0][1], "password": PASSWORD}).status_code == 400
    assert db.scalars(select(LoginSession)).all() == []


def test_bootstrap_creates_first_account_from_a_single_use_token(accounts):
    (owner, outsider, _), db = accounts
    assert owner.get("/auth/options").json()["setup_available"] is False
    raw = "bootstrap-test-secret-" + str(uuid4())
    db.add(AuthToken(token_hash=auth.digest(raw), purpose="bootstrap", expires_at=auth.now()+timedelta(hours=1)))
    db.commit()
    assert owner.get("/auth/options").json()["setup_available"] is True
    response = owner.post("/auth/bootstrap", json={
        "email": "owner@example.com", "name": "Owner", "password": PASSWORD, "token": raw,
    })
    assert response.status_code == 200, response.text
    owner.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert db.get(UserCreditAccount, UUID(response.json()["user"]["id"])).balance > 0
    assert owner.get("/knowledge-bases").json() == []
    assert owner.get("/conversations").json()["items"] == []
    assert owner.get("/auth/options").json()["setup_available"] is False
    assert db.get(AuthToken, auth.digest(raw)) is None
    register(outsider, "other@example.com", "Other")
    assert outsider.get("/knowledge-bases").json() == []


def test_first_registration_creates_personal_account(accounts):
    (owner, _, _), db = accounts
    result = register(owner)
    assert "workspaces" not in result
    assert "workspace_id" not in result
    assert "workspace_id" not in owner.get("/auth/me").json()
    assert db.get(UserCreditAccount, UUID(result["user"]["id"])).balance > 0
    assert not result["user"]["is_site_admin"]
    assert owner.get("/auth/options").json()["first_run"] is False


def test_oauth_error_state_cannot_create_session_or_link_identity(accounts, monkeypatch):
    (owner, _, _), db = accounts
    class InvalidState:
        async def authorize_access_token(self, _request):
            raise ValueError("state mismatch")
    monkeypatch.setattr(auth, "oidc_client", lambda: InvalidState())
    response = owner.get("/auth/oauth/callback?state=bad&code=bad", follow_redirects=False)
    assert response.status_code == 303
    assert "oauth_failed" in response.headers["location"]
    assert db.scalars(select(LoginSession)).all() == []
    assert db.scalars(select(ExternalIdentity)).all() == []


def test_revocation_stops_stream_before_late_delta(accounts, monkeypatch):
    (owner, _, _), db = accounts
    a = register(owner)
    from auth_helpers import configured_model
    configured_model(db, UUID(a["user"]["id"]), monkeypatch)
    id = library(owner)
    add_document(db, db.get(KnowledgeBase, id), "needle")
    conv = owner.post("/conversations", json={}).json()["id"]
    session_hash = auth.digest(owner.cookies.get(auth.COOKIE))
    class RevokingProvider:
        def complete_with_tools(self, _messages, _tools, *, tool_choice):
            with Session(db.get_bind()) as session:
                session.delete(session.get(LoginSession, session_hash))
                session.commit()
            return ToolResponse(content="SECRET AFTER REVOKE", tool_calls=[])
    monkeypatch.setattr(chat, "OpenAICompatibleChatProvider", lambda **_kwargs: RevokingProvider())
    from auth_helpers import model_id
    response = owner.post(f"/conversations/{conv}/messages", json={"provider_id": model_id(owner), "query": "needle", "request_id": str(uuid4())})
    assert response.status_code == 200
    assert "SECRET AFTER REVOKE" not in response.text
    from auth_helpers import generation_response
    revoked = generation_response(response)
    assert revoked.status_code in (401, 403)
    assert "SECRET AFTER REVOKE" not in revoked.text
    db.expire_all()
    assert db.get(Conversation, UUID(conv)).active_request_id is None


def oidc_server(monkeypatch, *, invalid_claim=None, email="oauth@example.com"):
    """Exercise Authlib's real state/PKCE/JWT validation against a local mock issuer."""
    settings = auth.get_settings().model_copy(update={
        "auth_secret_key": SecretStr("test-secret-at-least-32-characters"),
        "oidc_metadata_url": "https://issuer.example/.well-known/openid-configuration",
        "oidc_client_id": "test-client",
        "oidc_client_secret": SecretStr("test-client-secret"),
    })
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    client = auth.oidc_client()
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    state = {}

    def respond(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": "https://issuer.example",
                "authorization_endpoint": "https://issuer.example/authorize",
                "token_endpoint": "https://issuer.example/token",
                "jwks_uri": "https://issuer.example/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            })
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [key.as_dict()]})
        if request.url.path == "/token":
            params = parse_qs(request.content.decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(params["code_verifier"][0].encode()).digest()).decode().rstrip("=")
            assert challenge == state["code_challenge"][0]
            claims = {
                "iss": "https://issuer.example", "sub": "external-user-1", "aud": "test-client",
                "iat": int(time.time()), "exp": int(time.time())+300,
                "nonce": state["nonce"][0], "email": email, "email_verified": True, "name": "External User",
            }
            if invalid_claim:
                claims[invalid_claim] = "invalid"
            encoded = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key)
            return httpx.Response(200, json={"access_token": "access-token", "token_type": "Bearer", "id_token": encoded})
        raise AssertionError(f"Unexpected OIDC URL: {request.url}")

    client.client_kwargs["transport"] = httpx.MockTransport(respond)
    monkeypatch.setattr(auth, "oidc_client", lambda: client)
    return state


def test_oidc_code_flow_validates_pkce_signature_and_creates_account(accounts, monkeypatch):
    (owner, _, _), db = accounts
    state = oidc_server(monkeypatch)
    start = owner.post("/auth/oauth/start", json={})
    assert start.status_code == 200, start.text
    state.update(parse_qs(urlparse(start.json()["url"]).query))
    assert state["code_challenge_method"] == ["S256"]
    assert state["scope"] == ["openid email profile"]
    response = owner.get("/auth/oauth/callback", params={"state": state["state"][0], "code": "test-code"}, follow_redirects=False)
    assert response.status_code == 303 and "auth_error" not in response.headers["location"], response.headers
    me = owner.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "oauth@example.com"
    assert me.json()["user"]["has_password"] is False
    assert len(db.scalars(select(ExternalIdentity)).all()) == 1
    # The authorization state was consumed and cannot be replayed.
    replay = owner.get("/auth/oauth/callback", params={"state": state["state"][0], "code": "test-code"}, follow_redirects=False)
    assert "oauth_failed" in replay.headers["location"]


@pytest.mark.parametrize("claim", ["nonce", "iss", "aud"])
def test_oidc_invalid_id_token_never_creates_an_account(accounts, monkeypatch, claim):
    (owner, _, _), db = accounts
    state = oidc_server(monkeypatch, invalid_claim=claim)
    start = owner.post("/auth/oauth/start", json={})
    state.update(parse_qs(urlparse(start.json()["url"]).query))
    response = owner.get("/auth/oauth/callback", params={"state": state["state"][0], "code": "test-code"}, follow_redirects=False)
    assert "oauth_failed" in response.headers["location"]
    assert db.scalars(select(User)).all() == []
    assert db.scalars(select(ExternalIdentity)).all() == []
    assert db.scalars(select(LoginSession)).all() == []


def test_oidc_does_not_merge_by_email_but_supports_explicit_link(accounts, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    assert owner.post("/auth/logout").status_code == 204
    state = oidc_server(monkeypatch, email="owner@example.com")
    start = owner.post("/auth/oauth/start", json={})
    state.update(parse_qs(urlparse(start.json()["url"]).query))
    response = owner.get("/auth/oauth/callback", params={"state": state["state"][0], "code": "test-code"}, follow_redirects=False)
    assert "link_required" in response.headers["location"]
    assert db.scalars(select(ExternalIdentity)).all() == []
    logged_in = owner.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    owner.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]
    # Linking now requires a real mailbox proof even while rollout policy is off.
    from app.identity import email_auth, site_auth
    mail_settings = auth.get_settings().model_copy(update={"smtp_host": "smtp.example.com", "smtp_from": "mail@example.com"})
    monkeypatch.setattr(site_auth, "get_settings", lambda: mail_settings)
    monkeypatch.setattr(email_auth, "get_settings", lambda: mail_settings)
    monkeypatch.setattr(email_auth, "email_rate_limit", lambda *_a, **_k: None)
    codes = []
    monkeypatch.setattr(email_auth, "send_code_mail", lambda db, email, code, purpose="verify": codes.append(code))
    assert owner.post("/auth/email/send-code", json={"purpose": "verify"}).status_code == 200
    verified = owner.post("/auth/email/verify", json={"code": codes[-1]})
    assert verified.status_code == 200
    owner.headers["X-CSRF-Token"] = verified.json()["csrf_token"]
    start = owner.post("/auth/oauth/start", json={"link": True})
    state.update(parse_qs(urlparse(start.json()["url"]).query))
    response = owner.get("/auth/oauth/callback", params={"state": state["state"][0], "code": "test-code"}, follow_redirects=False)
    assert "account=1" in response.headers["location"]
    assert owner.get("/auth/me").json()["user"]["oauth_linked"] is True
    assert len(db.scalars(select(User)).all()) == 1
