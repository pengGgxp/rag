"""Explicit, bounded live acceptance in a new isolated workspace. Never prints credentials."""
import argparse
import hashlib
import json
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from httpx_sse import EventSource
from sqlalchemy import delete, select

from app.identity import auth
from app.infra.config import get_settings
from app.billing.credits import ensure_account
from app.infra.db import SessionLocal
from app.models import (
    ChatMessage, ContextCheckpoint, Conversation, LoginSession, Membership, ModelBillingSettings,
    SiteModelProvider, ModelConnection, User, UsageEvent, Workspace,
)
from app.operations.site_audit import record


def invariant(db):
    settings = db.get(ModelBillingSettings, 1)
    assert settings and settings.shared_pricing_enabled and not settings.enabled
    return hashlib.sha256(json.dumps({
        "billing": [settings.enabled, settings.shared_pricing_enabled,
                    str(settings.default_provider_id), settings.template],
        "providers": [(str(row.id), row.prices, row.thinking_config, row.enabled,
                       str(row.connection_id), row.model)
                      for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.id))],
        "connections": [(str(c.id), c.base_url, c.encrypted_api_key, c.version, c.enabled)
            for c in db.scalars(select(ModelConnection).order_by(ModelConnection.id))],
    }, sort_keys=True).encode()).hexdigest()


def stream(client, path, payload):
    started, first, events = time.monotonic(), None, []
    with client.stream("POST", path, json=payload) as response:
        assert response.status_code == 200, f"HTTP {response.status_code}"
        for event in EventSource(response).iter_sse():
            if not event.data:
                continue
            data = event.json()
            if event.event == "delta" and first is None:
                first = time.monotonic() - started
            events.append((event.event, data))
    assert events and events[-1][0] == "done", "Missing terminal event"
    return events, first, time.monotonic() - started


def run(path):
    assert not path.exists(), "Use a new state file; live calls must not be accidentally repeated"
    settings = get_settings()
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with SessionLocal() as db:
        before = invariant(db)
        workspace = Workspace(name="Issue45 流式与上下文验收")
        user = User(email=f"issue45-{uuid4().hex}@example.com", name="Issue45 验收",
                    email_verified_at=auth.now(), is_site_admin=True)
        db.add_all([workspace, user])
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        ensure_account(db, workspace.id)
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id,
            workspace_id=workspace.id, expires_at=auth.now() + timedelta(hours=1)))
        record(db, user, "verification.started", "workspace", workspace.id,
               details={"reason_code": "issue45_controlled_acceptance"})
        db.commit()
        state = {"cookie": raw, "csrf": csrf, "user_id": str(user.id), "workspace_id": str(workspace.id),
                 "api_url": settings.api_public_url, "frontend_url": settings.frontend_url,
                 "before": before, "results": []}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(state, file)
    with httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=180,
            cookies={auth.COOKIE: raw}, headers={"X-RAG-Request": "1", "X-CSRF-Token": csrf,
                                               "Origin": settings.frontend_url}) as client:
        providers = client.get("/models/available").json()["providers"]
        available = [row for row in providers if row["available"]]
        assert 0 < len(available) <= 2, "This bounded check expects the two verified configured models"
        for choice in available:
            detail = client.get(f"/site/admin/llm-providers/{choice['id']}").json()
            assert detail["model"] in ("deepseek-flash", "qwen3.8-max-0902"), "Verify official capacity first"
            payload = {key: detail[key] for key in (
                "key", "name", "base_url", "model", "timeout_seconds", "enabled", "version", "thinking")}
            payload.update(context_capacity=1_000_000, max_output_tokens=8192)
            updated = client.put(f"/site/admin/llm-providers/{choice['id']}", json=payload)
            assert updated.status_code == 200, f"Budget update: HTTP {updated.status_code}"
            conversation = client.post("/conversations", json={})
            assert conversation.status_code == 201
            identifier = conversation.json()["id"]
            events, first, duration = stream(client, f"/conversations/{identifier}/messages", {
                "request_id": str(uuid4()), "provider_id": choice["id"],
                "query": "这是隔离验收。请先简短说明你将回查记录，然后调用 read_conversation_history"
                         "读取0到1条记录；再说明将复核，并再次调用同一工具读取0到1条。"
                         "最后用20字以内说明已完成，不需要其他工具。",
            })
            final = events[-1][1]
            assert final["status"] == "completed", f"{choice['model']}: status={final['status']}"
            assert final["model"] == choice["model"] and first is not None
            tools = final["tool_events"]
            assert len(tools) >= 2 and all(row["status"] == "completed" for row in tools), "Two real tool calls required"
            assert len({row["call_id"] for row in tools}) == len(tools)
            result = {"model": choice["model"], "conversation_id": identifier,
                "request_id": final["request_id"], "first_delta_seconds": round(first, 3),
                "done_seconds": round(duration, 3), "tools": len(tools),
                "block_order": [row["type"] for row in final["display_blocks"]]}
            with SessionLocal() as db:
                usage = db.scalars(select(UsageEvent).where(
                    UsageEvent.request_id == UUID(final["request_id"]))).all()
                assert usage and all(row.metadata_json.get("source") == "provider" for row in usage)
                result["usage_records"] = len(usage)
                result["charged_credits"] = sum(row.credits for row in usage)
            state["results"].append(result)
            path.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        identifier = state["results"][0]["conversation_id"]
        with SessionLocal() as db:
            conversation = db.get(Conversation, UUID(identifier), with_for_update=True)
            for index in range(8):
                db.add(ChatMessage(conversation_id=conversation.id, sequence=conversation.next_sequence + index,
                    role="user" if index % 2 == 0 else "assistant", status="completed",
                    content=f"隔离验收历史 {index}：" + "目标是验证工具顺序与上下文整理，约束是不修改商业价格和默认模型。" * 35))
            conversation.next_sequence += 8
            db.commit()
        request_id = uuid4()
        events, _, duration = stream(client, f"/conversations/{identifier}/compact", {
            "request_id": str(request_id), "provider_id": available[0]["id"]})
        assert events[-1][1]["status"] == "completed", "Live compaction failed"
        with SessionLocal() as db:
            checkpoint = db.scalar(select(ContextCheckpoint).where(ContextCheckpoint.request_id == request_id))
            assert checkpoint and checkpoint.estimated_after < checkpoint.estimated_before
            assert invariant(db) == before, "Commercial configuration changed"
            state["compaction"] = {"request_id": str(request_id), "model": checkpoint.model,
                "before": checkpoint.estimated_before, "after": checkpoint.estimated_after,
                "seconds": round(duration, 3)}
        state["finished"] = True
        path.write_text(json.dumps(state), encoding="utf-8")
        print(json.dumps({"compaction": state["compaction"], "commercial_configuration": "unchanged"},
                         ensure_ascii=False), flush=True)


def cleanup(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    with SessionLocal() as db:
        user = db.get(User, UUID(state["user_id"]))
        user.is_site_admin, user.is_suspended = False, True
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        record(db, user, "verification.completed", "workspace", state["workspace_id"],
               details={"reason_code": "issue45_sessions_revoked"})
        assert invariant(db) == state["before"]
        db.commit()
    state.pop("cookie", None)
    state.pop("csrf", None)
    path.write_text(json.dumps(state), encoding="utf-8")
    print("Temporary administrator revoked; sessions removed; accounting records retained.")


def compact_existing(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    assert not state.get("finished") and state.get("results"), "No interrupted acceptance to resume"
    with httpx.Client(base_url=state["api_url"].rstrip("/"), timeout=180,
            cookies={auth.COOKIE: state["cookie"]}, headers={"X-RAG-Request": "1",
                "X-CSRF-Token": state["csrf"], "Origin": state["frontend_url"]}) as client:
        models = client.get("/models/available").json()["providers"]
        provider = next(row for row in models if row["model"] == state["results"][0]["model"])
        request_id = uuid4()
        events, _, duration = stream(client, f"/conversations/{state['results'][0]['conversation_id']}/compact", {
            "request_id": str(request_id), "provider_id": provider["id"]})
        assert events[-1][1]["status"] == "completed", "Live compaction failed"
        with SessionLocal() as db:
            checkpoint = db.scalar(select(ContextCheckpoint).where(ContextCheckpoint.request_id == request_id))
            assert checkpoint and checkpoint.estimated_after < checkpoint.estimated_before
            usage = db.scalars(select(UsageEvent).where(UsageEvent.request_id == request_id)).all()
            assert usage and all(row.metadata_json.get("source") == "provider" for row in usage)
            assert invariant(db) == state["before"]
            state["compaction"] = {"request_id": str(request_id), "model": checkpoint.model,
                "before": checkpoint.estimated_before, "after": checkpoint.estimated_after,
                "seconds": round(duration, 3), "charged_credits": sum(row.credits for row in usage)}
        state["finished"] = True
        path.write_text(json.dumps(state), encoding="utf-8")
        print(json.dumps({"compaction": state["compaction"], "commercial_configuration": "unchanged"},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--compact-existing", action="store_true")
    args = parser.parse_args()
    if args.cleanup:
        cleanup(args.state_file)
    elif args.compact_existing:
        compact_existing(args.state_file)
    else:
        run(args.state_file)
