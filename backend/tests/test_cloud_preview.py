import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cloud_computer as cloud, cloud_preview as preview, preview_gateway as gateway
from app.auth import aware
from app.config import get_settings
from app.models import CloudPreview, CloudPreviewGrant, LoginSession
from test_auth import accounts, register
from test_cloud_computer import setup


def prepare(accounts, monkeypatch):
    client, db, conversation, access, fake, factory = setup(accounts, monkeypatch, rate="0")
    monkeypatch.setattr(get_settings(), "preview_enabled", True)
    monkeypatch.setattr(fake, "start_preview", lambda sandbox, identifier, payload: fake.start(sandbox, identifier, payload["command"]), raising=False)
    from app.cloud_computer_adapter import preview_ingress_port
    monkeypatch.setattr(fake, "preview_target", lambda sandbox, port, identifier: (f"{preview_ingress_port(identifier)}-synthetic.cn-hangzhou.sandbox.aliyuncs.com", "synthetic-traffic-token"), raising=False)
    monkeypatch.setattr(fake, "preview_alive", lambda *_: True, raising=False)
    monkeypatch.setattr(fake, "stop_preview", lambda *_: None, raising=False)
    request = uuid4()
    args = {"command": "python3 -m http.server 8080", "preview": {"command": "python3 -m http.server 8080", "port": 8080, "name": "合成预览"}}
    command = cloud.submit(db, access, conversation.id, request, args)
    cloud.run(command.id, factory)
    db.expire_all()
    row = db.scalar(select(CloudPreview))
    computer = db.get(cloud.Computer, row.session_id)
    return client, db, conversation, access, fake, factory, command, row, computer, request, args


def test_preview_ready_dedup_conflict_permissions(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, command, row, computer, request, args = prepare(accounts, monkeypatch)
    assert row.status == "ready"
    assert fake.starts == 1
    assert cloud.submit(db, access, conversation.id, request, args).id == command.id
    assert fake.starts == 1
    with pytest.raises(HTTPException) as error:
        cloud.submit(db, access, conversation.id, uuid4(), args)
    assert error.value.status_code == 409
    db.rollback()
    data = client.get(f"/previews/{row.id}")
    assert data.status_code == 200
    assert "synthetic-traffic-token" not in data.text and "upstream_host" not in data.text
    assert data.json()["computer"]["max_ends_at"]
    other = accounts[0][1]
    register(other, "preview-other@example.com")
    assert other.get(f"/previews/{row.id}").status_code == 404
    assert other.post(f"/previews/{row.id}/access", json={}).status_code == 404


def test_lease_idle_and_max_lifetime(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, command, row, computer, *_ = prepare(accounts, monkeypatch)
    start = cloud.now()
    monkeypatch.setattr(cloud, "now", lambda: start)
    monkeypatch.setattr(preview, "now", lambda: start)
    assert client.post(f"/previews/{row.id}/heartbeat", json={}).status_code == 200
    db.expire_all()
    assert aware(row.lease_until) == start + timedelta(seconds=60)
    # Ordinary status reads leave the activity clock unchanged.
    before = computer.last_activity_at
    client.get(f"/previews/{row.id}")
    db.refresh(computer)
    assert computer.last_activity_at == before
    monkeypatch.setattr(cloud, "now", lambda: start+timedelta(seconds=630))
    cloud.maintain_one(computer.id, factory)
    db.refresh(computer)
    assert not computer.ended_at
    monkeypatch.setattr(cloud, "now", lambda: start+timedelta(seconds=661))
    cloud.maintain_one(computer.id, factory)
    db.refresh(computer); db.refresh(row)
    assert computer.ended_at and row.ended_at
    assert client.post(f"/previews/{row.id}/heartbeat", json={}).status_code == 410


def test_ticket_exchange_host_binding_revoke_and_http_proxy(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, command, row, computer, *_ = prepare(accounts, monkeypatch)
    monkeypatch.setattr(gateway, "SessionLocal", factory)
    grant = client.post(f"/previews/{row.id}/access", json={})
    assert grant.status_code == 200
    host = f"{row.id.hex}.preview.str1ct.top"
    browser = TestClient(gateway.app, base_url=f"https://{host}")
    token = grant.json()["url"].split("ticket=")[1]
    response = browser.get(f"/_zk_preview/auth?ticket={token}", follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Domain=" not in response.headers["set-cookie"]
    assert browser.get(f"/_zk_preview/auth?ticket={token}").status_code == 401
    assert browser.get("/", headers={"host": f"{uuid4().hex}.preview.str1ct.top"}).status_code == 401
    assert browser.get("/", headers={"Upgrade": "websocket"}).status_code == 426
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"synthetic page"), headers={"content-type": "text/html", "set-cookie": "app=ok; Domain=bad.example; Path=/"})
    original = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    response = browser.post("/form?x=1", content=b"field=value", headers={"Authorization": "platform-secret", "X-Forwarded-Host": "unsafe", "Cookie": f"{gateway.COOKIE}={browser.cookies.get(gateway.COOKIE)}; rag_session=platform-cookie"})
    assert response.status_code == 200 and response.content == b"synthetic page"
    assert calls[0].headers["e2b-traffic-access-token"] == "synthetic-traffic-token"
    assert "authorization" not in calls[0].headers and "cookie" not in calls[0].headers
    assert "x-forwarded-host" not in calls[0].headers
    assert calls[0].content == b"field=value"
    assert "Domain=" not in response.headers["set-cookie"]
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert browser.post("/form", content=b"x", headers={"content-length": str(gateway.MAX_BODY+1)}).status_code == 413
    computer.status = "stopping"; db.commit()
    assert browser.get("/").status_code == 410


def test_stop_service_keeps_computer_and_failure_does_not_claim_ready(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, command, row, computer, *_ = prepare(accounts, monkeypatch)
    result = preview.stop(db, access, row, computer)
    assert result["status"] == "stopped"
    assert fake.kills == 0 and not computer.ended_at
    assert client.post(f"/previews/{row.id}/access", json={}).status_code == 410
    assert preview.stop(db, access, row, computer)["status"] == "stopped"
    fake.result = {"exit_code": 1, "stdout": "", "stderr": "port in use"}
    args = {"command": "bad service", "preview": {"command": "bad service", "port": 8080, "name": "失败示例"}}
    command2 = cloud.submit(db, access, conversation.id, uuid4(), args)
    cloud.run(command2.id, factory)
    db.expire_all()
    failed = db.scalar(select(CloudPreview).where(CloudPreview.command_id == command2.id))
    assert failed.status == "failed" and failed.ended_at and preview.read(failed)["url"] is None


@pytest.mark.parametrize("port", [22, 443, 5432, 6379, 50005])
def test_reject_management_ports(port):
    with pytest.raises(ValueError):
        preview.Start(command="echo test", port=port, name="test")


def test_gateway_redirect_and_cookie_filter():
    target = {"host": "8080-synthetic.cn-hangzhou.sandbox.aliyuncs.com", "origin": "https://example.preview.str1ct.top"}
    request = httpx.Request("GET", "https://"+target["host"]+"/nested/page")
    response = httpx.Response(302, headers={"location": "../next"}, request=request)
    assert gateway.response_headers(response, target)["location"] == target["origin"]+"/next"
    response = httpx.Response(302, headers={"location": "https://external.example"}, request=request)
    with pytest.raises(HTTPException):
        gateway.response_headers(response, target)


def test_gateway_renders_html_despite_provider_attachment_header():
    target = {"host": "8080-synthetic.cn-hangzhou.sandbox.aliyuncs.com", "origin": "https://example.preview.str1ct.top"}
    response = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", "content-disposition": "attachment"})
    assert "content-disposition" not in gateway.response_headers(response, target)
    response = httpx.Response(200, headers={"content-type": "application/pdf", "content-disposition": "attachment; filename=report.pdf"})
    assert gateway.response_headers(response, target)["content-disposition"] == "attachment; filename=report.pdf"


def test_max_deadline_expired_grant_and_logout(accounts, monkeypatch):
    client, db, conversation, access, fake, factory, command, row, computer, *_ = prepare(accounts, monkeypatch)
    monkeypatch.setattr(gateway, "SessionLocal", factory)
    ticket = client.post(f"/previews/{row.id}/access", json={}).json()["url"].split("ticket=")[1]
    grant = db.get(CloudPreviewGrant, preview.digest(ticket))
    grant.expires_at = cloud.now()-timedelta(seconds=1); db.commit()
    with pytest.raises(HTTPException) as error:
        gateway.check_grant(row.id, ticket, "ticket")
    assert error.value.status_code == 401
    ticket = client.post(f"/previews/{row.id}/access", json={}).json()["url"].split("ticket=")[1]
    login = db.get(LoginSession, access.session_hash)
    login.expires_at = cloud.now()-timedelta(seconds=1); db.commit()
    with pytest.raises(HTTPException) as error:
        gateway.check_grant(row.id, ticket, "ticket")
    assert error.value.status_code == 403
    login.expires_at = cloud.now()+timedelta(hours=1); db.commit()
    deadline = aware(computer.started_at)+timedelta(seconds=computer.snapshot["max_seconds"])
    row.lease_until = deadline+timedelta(minutes=5)
    db.commit()
    monkeypatch.setattr(cloud, "now", lambda: deadline+timedelta(seconds=1))
    cloud.maintain_one(computer.id, factory)
    db.refresh(computer)
    assert computer.ended_at
