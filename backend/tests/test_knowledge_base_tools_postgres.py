"""Exercise migrated PostgreSQL receipts and concurrent delivery; no external models."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.identity import auth
from app.integrations.runtime import ExtensionRuntime
from app.models import (AuditLog, ChatMessage, Conversation, KnowledgeBase,
                        KnowledgeBaseToolExecution, LoginSession, User)
from test_auth_postgres import auth_postgres


def test_concurrent_create_and_update_receipts_are_atomic(auth_postgres):
    engine = auth_postgres
    with Session(engine) as db:
        user = auth.create_user(db, f"kb-{uuid4().hex}@example.com", "临时用户", "test-password-1234")
        account_id = user.id
        token = uuid4().hex
        db.add(LoginSession(token_hash=auth.digest(token), user_id=user.id,
            csrf_token="test-only", expires_at=auth.now() + timedelta(hours=1)))
        conversation = Conversation(owner_id=user.id, account_id=account_id, title="临时会话")
        db.add(conversation); db.flush()
        request_id = uuid4()
        message = ChatMessage(conversation_id=conversation.id, sequence=2, role="assistant", request_id=request_id)
        db.add(message); db.commit()
        access = auth.Access(user.id, auth.digest(token))
        message_id = message.id
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    args = {"name": "并发知识库"}
    barrier = Barrier(4)
    def deliver(_):
        runtime = ExtensionRuntime(factory, access, message_id=message_id, request_id=request_id)
        barrier.wait(timeout=20)
        return runtime.execute("create_knowledge_base", args, call_id="tool-1-create")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, range(4)))
    assert all(result == results[0] and not result.get("error") for result in results), results
    identifier = results[0]["knowledge_base"]["id"]
    barrier = Barrier(4)
    def update(_):
        runtime = ExtensionRuntime(factory, access, message_id=message_id, request_id=request_id)
        barrier.wait(timeout=20)
        return runtime.execute("update_knowledge_base", {"knowledge_base_id": identifier, "name": "并发更新"}, call_id="tool-2-update")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(update, range(4)))
    assert all(result == results[0] and not result.get("error") for result in results), results
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.account_id == account_id)) == 1
        assert db.scalar(select(func.count()).select_from(KnowledgeBaseToolExecution).where(KnowledgeBaseToolExecution.message_id == message_id)) == 2
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.owner_user_id == account_id,
            AuditLog.action.in_(["knowledge_base.created", "knowledge_base.updated"]))) == 2
        # Revocation must apply to replay, even with an original admin Access object.
        db.get(User, access.user_id).is_suspended = True
        db.commit()
    with pytest.raises(HTTPException) as denied:
        ExtensionRuntime(factory, access, message_id=message_id, request_id=request_id)
    assert denied.value.status_code == 403
