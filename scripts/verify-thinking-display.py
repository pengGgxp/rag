"""Bounded live verification in an isolated workspace; never prints credentials or thinking."""
import argparse
import hashlib
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from httpx_sse import EventSource
from sqlalchemy import delete, select

from app import auth
from app.config import get_settings
from app.credits import ensure_account
from app.db import SessionLocal
from app.models import ChatMessage, LoginSession, Membership, ModelBillingSettings, ModelConnection, SiteModelProvider, ThinkingTest, User, Workspace
from app.site_audit import record


def invariant(db):
    settings = db.get(ModelBillingSettings, 1)
    return hashlib.sha256(json.dumps({
        "billing": [settings.enabled, settings.shared_pricing_enabled, str(settings.default_provider_id), settings.template],
        "providers": [(str(row.id), row.prices, row.thinking_config, row.context_capacity, row.max_output_tokens,
            row.version, row.enabled, str(row.connection_id))
            for row in db.scalars(select(SiteModelProvider).order_by(SiteModelProvider.id))],
        "connections": [(str(c.id), c.base_url, c.encrypted_api_key, c.version, c.enabled)
            for c in db.scalars(select(ModelConnection).order_by(ModelConnection.id))],
    }, sort_keys=True).encode()).hexdigest()


def create_state(path, issue=49):
    if path.exists():
        raise SystemExit("Use a fresh state path; never replay live calls accidentally")
    raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with SessionLocal() as db:
        workspace = Workspace(name=f"Issue{issue} 模型受控验收")
        user = User(email=f"model{issue}-{uuid4().hex}@example.com", name="模型受控验收",
            email_verified_at=auth.now(), is_site_admin=True)
        db.add_all([workspace, user]); db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        ensure_account(db, workspace.id, initial=1_000_000)
        db.add(LoginSession(token_hash=auth.digest(raw), csrf_token=csrf, user_id=user.id,
            workspace_id=workspace.id, expires_at=auth.now() + timedelta(hours=1)))
        record(db, user, "verification.started", "workspace", workspace.id,
            details={"reason_code": f"issue{issue}_controlled_acceptance"})
        state = {"cookie": raw, "csrf": csrf, "user_id": str(user.id), "workspace_id": str(workspace.id),
            "before": invariant(db), "results": [], "issue": issue}
        db.commit()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(state, file)
    return state


def run(path, *, resume=False, issue=49, selected_models=None):
    state = json.loads(path.read_text(encoding="utf-8")) if resume else create_state(path, issue)
    settings = get_settings()
    with httpx.Client(base_url=settings.api_public_url.rstrip("/"), timeout=180,
        cookies={auth.COOKIE: state["cookie"]}, headers={"X-RAG-Request": "1", "X-CSRF-Token": state["csrf"],
        "Origin": settings.frontend_url}) as client:
        models = client.get("/models/available").json()["providers"]
        models = [row for row in models if row["available"] and row["thinking"]["kind"] == "effort"]
        if selected_models:
            models = [row for row in models if row["model"] in selected_models]
            assert {row["model"] for row in models} == set(selected_models), "Selected models are not all available"
        assert 0 < len(models) <= 2, "This bounded verification expects at most two configured models"
        for model in models:
            if any(row["model"] == model["model"] for row in state["results"]):
                continue
            detail = client.get(f"/site/admin/llm-providers/{model['id']}").json()
            discovered = client.post(f"/site/admin/model-connections/{detail['connection_id']}/models/discover",
                json={"version": detail["connection_version"]})
            assert discovered.status_code in (200, 502), "Unexpected connection discovery response"
            request = {"request_id": str(uuid4()), "version": detail["version"],
                "connection_version": detail["connection_version"],
                "thinking_config_version": detail["thinking_version"], "option_id": detail["thinking"]["default_option_id"]}
            with SessionLocal() as db:
                previous = db.scalar(select(ThinkingTest).where(
                    ThinkingTest.workspace_id == UUID(state["workspace_id"]),
                    ThinkingTest.provider_id == UUID(model["id"]),
                ))
                if previous:
                    assert previous.status == "completed", "Do not automatically repeat an unfinished probe"
                    request["request_id"] = str(previous.request_id)
            probe = client.post(f"/site/admin/llm-providers/{model['id']}/thinking/test", json=request)
            assert probe.status_code == 200
            result = probe.json()
            assert client.post(f"/site/admin/llm-providers/{model['id']}/thinking/test", json=request).json() == result
            conversation = client.post("/conversations", json={}).json()["id"]
            names, thinking_chars, last = [], 0, None
            with client.stream("POST", f"/conversations/{conversation}/messages", json={
                "request_id": str(uuid4()), "provider_id": model["id"],
                "thinking_option_id": request["option_id"], "thinking_config_version": detail["thinking_version"],
                "query": "这是隔离验收，请调用一次 read_conversation_history 回查第0到1条记录，然后只回答“已核对”。不要调用其他工具。",
            }) as response:
                assert response.status_code == 200
                for event in EventSource(response).iter_sse():
                    if not event.data:
                        continue
                    data = event.json()
                    names.append(event.event)
                    if event.event == "thinking_delta":
                        thinking_chars += len(data["text"])
                    if event.event == "done":
                        last = data
            assert last and last["status"] == "completed", "Conversation did not complete"
            assert thinking_chars > 0 and "tool_started" in names, "Thinking/tools were not observed"
            assert all("text" not in block for block in last["display_blocks"] if block["type"] == "thinking")
            restored = client.get(f"/conversations/{conversation}/messages/{last['id']}/thinking").json()
            assert sum(len(block["text"]) for block in restored["blocks"]) == thinking_chars
            with SessionLocal() as db:
                row = db.get(ChatMessage, UUID(last["id"]))
                assert row.encrypted_display_thinking and not row.encrypted_display_thinking.startswith("[")
                assert invariant(db) == state["before"], "Commercial/model configuration changed"
            record_result = {"model": model["model"], "conversation_id": conversation,
                "discovery_status": discovered.status_code,
                "discovered_count": len(discovered.json().get("models", [])),
                "connection_version": detail["connection_version"],
                "thinking_characters": thinking_chars, "block_order": [b["type"] for b in last["display_blocks"]],
                "diagnostics": last["thinking_diagnostics"], "probe_completed": result["completed"],
                "probe_received_thinking": result.get("received_thinking"), "probe_reasoning_tokens": result.get("reasoning_tokens"),
                "probe_credits": result.get("credits"), "probe_error": result.get("error")}
            state["results"].append(record_result)
            path.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps(record_result, ensure_ascii=False), flush=True)
        state["finished"] = True
        path.write_text(json.dumps(state), encoding="utf-8")


def cleanup(path):
    state = json.loads(path.read_text(encoding="utf-8"))
    with SessionLocal() as db:
        user = db.get(User, UUID(state["user_id"]))
        user.is_site_admin, user.is_suspended = False, True
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
        assert invariant(db) == state["before"]
        record(db, user, "verification.completed", "workspace", state["workspace_id"],
            details={"reason_code": f"issue{state.get('issue', 49)}_sessions_revoked"})
        db.commit()
    print("Verification sessions revoked; model and commercial configuration unchanged.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["run", "cleanup"])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--issue", type=int, default=49)
    parser.add_argument("--model", action="append", help="Limit paid verification to these exact model names")
    args = parser.parse_args()
    if args.operation == "run":
        run(args.state, resume=args.resume, issue=args.issue, selected_models=args.model)
    else:
        cleanup(args.state)
