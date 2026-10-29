"""Bounded live COS/document acceptance in disposable accounts; never prints credentials."""
import argparse
import hashlib
import json
import struct
import zlib
from uuid import UUID, uuid4

import httpx
from httpx_sse import EventSource
from sqlalchemy import delete, select

from app import auth
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    LoginSession, Membership, ModelBillingSettings, ModelConnection, SiteModelProvider, UsageEvent, User,
)


def invariant(db):
    billing = db.get(ModelBillingSettings, 1)
    return hashlib.sha256(json.dumps({
        "billing": [billing.enabled, billing.shared_pricing_enabled, billing.pricing_paused,
                    str(billing.default_provider_id), billing.template],
        "models": [(str(row.id), row.version, row.prices, row.supports_images, row.enabled)
                   for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.id))],
        "connections": [(str(row.id), row.version) for row in
                        db.scalars(select(ModelConnection).order_by(ModelConnection.id))],
    }, sort_keys=True).encode()).hexdigest()


def png():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(
        b"IDAT", zlib.compress(b"\x00\x25\x63\xeb")) + chunk(b"IEND", b"")


def run():
    settings, created, clients = get_settings(), [], []
    with SessionLocal() as db:
        before = invariant(db)
    try:
        for name in ("member", "outsider"):
            with SessionLocal() as db:
                user, workspace = auth.create_user(db, f"issue56-{uuid4().hex}@example.com", f"附件验收 {name}")
                user.email_verified_at = auth.now()
                db.flush()
                db.scalar(select(Membership).where(Membership.user_id == user.id)).role = "member"
                response = auth.login_response(db, user, workspace_id=workspace)
                created.append(user.id)
                token = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
                clients.append(httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=100,
                    cookies={auth.COOKIE: token}, headers={"X-RAG-Request": "1", "Origin": settings.frontend_url,
                    "X-CSRF-Token": json.loads(response.body)["csrf_token"]}))
        member, outsider = clients
        assert member.get("/site/admin/llm-providers").status_code == 403
        models = member.get("/models/available")
        assert models.status_code == 200
        assert all(key not in models.text for key in ("encrypted_api_key", "base_url", "credits_per_unit"))
        choice = next(row for row in models.json()["providers"] if row["available"])
        image = png()
        uploaded = member.post("/chat/attachments", files={"file": ("acceptance.png", image, "image/png")})
        assert uploaded.status_code == 201, f"Image upload HTTP {uploaded.status_code}"
        identifier = uploaded.json()["id"]
        path = f"/chat/attachments/{identifier}"
        download = member.get(path + "/content")
        assert download.status_code == 200 and download.content == image
        assert outsider.get(path + "/content").status_code == 404
        assert outsider.delete(path).status_code == 404
        if not choice["supports_images"]:
            conversation = member.post("/conversations", json={}).json()
            rejected = member.post(f"/conversations/{conversation['id']}/messages", json={
                "request_id": str(uuid4()), "provider_id": choice["id"], "attachment_ids": [identifier]})
            assert rejected.status_code == 422
            assert member.delete(f"/conversations/{conversation['id']}").status_code == 204
        assert member.delete(path).status_code == 204
        assert member.get(path + "/content").status_code == 404
        text = "验收资料：项目代号为青穗，发布日期为十月三十日。"
        document = member.post("/chat/attachments", files={"file": (
            "acceptance.txt", text.encode(), "text/plain")})
        assert document.status_code == 201, f"Document upload HTTP {document.status_code}"
        attachment = document.json()
        assert member.get(f"/chat/attachments/{attachment['id']}/content").content == text.encode()
        conversation = member.post("/conversations", json={})
        assert conversation.status_code == 201
        cid, request_id = conversation.json()["id"], uuid4()
        terminal, deltas = None, 0
        with member.stream("POST", f"/conversations/{cid}/messages", json={
            "request_id": str(request_id), "provider_id": choice["id"],
            "attachment_ids": [attachment["id"]],
            "query": "请只根据附件告诉我项目代号，只回答代号，不要调用工具。"
        }) as response:
            assert response.status_code == 200, f"Question HTTP {response.status_code}"
            for event in EventSource(response).iter_sse():
                if event.event == "delta":
                    deltas += 1
                if event.event == "done":
                    terminal = event.json()
        assert terminal and terminal["status"] == "completed", "Real document answer did not complete"
        assert "青穗" in terminal["content"], "Answer did not use attachment text"
        history = member.get(f"/conversations/{cid}").json()
        assert history["messages"][0]["attachments"][0]["id"] == attachment["id"]
        assert outsider.get(f"/chat/attachments/{attachment['id']}/content").status_code == 404
        with SessionLocal() as db:
            usage = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
            assert usage and all(row.metadata_json.get("source") == "provider" for row in usage)
            assert invariant(db) == before
            charged = sum(row.credits for row in usage)
        print(json.dumps({"passed": True, "storage": "real COS upload/read/delete",
            "cross_workspace_denied": True, "member_admin_denied": True,
            "document_answer": "real provider completed", "model": choice["model"],
            "deltas": deltas, "usage_records": len(usage), "charged_credits": str(charged),
            "commercial_configuration": "unchanged", "real_vision": "not verified"}, ensure_ascii=False))
    finally:
        for client in clients:
            client.close()
        with SessionLocal() as db:
            for identifier in created:
                db.get(User, identifier).is_suspended = True
                db.execute(delete(LoginSession).where(LoginSession.user_id == identifier))
            assert invariant(db) == before
            db.commit()
        print("Isolated accounts suspended; sessions revoked; accounting preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", required=True, action="store_true")
    parser.parse_args()
    run()
