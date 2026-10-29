from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth
from app.chat_attachments import select_attachments
from app.llm import ChatProviderConfig
from app.models import ChatAttachment, ChatMessage, Conversation, Membership, User, Workspace
from test_auth_postgres import auth_postgres


def test_concurrent_binding_is_once_and_preserves_original(auth_postgres):
    with Session(auth_postgres) as db:
        workspace = Workspace(name="Attachment isolation")
        user = User(email=f"attachment-{uuid4()}@example.com", name="Attachment test")
        db.add_all([workspace, user]); db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="admin"))
        conversations = [Conversation(owner_id=user.id, workspace_id=workspace.id) for _ in range(2)]
        db.add_all(conversations); db.flush()
        row = ChatAttachment(workspace_id=workspace.id, user_id=user.id, filename="facts.txt",
            content_type="text/plain", size_bytes=5, content_hash="a" * 64, object_key=f"_chat/{workspace.id}/{uuid4()}.txt",
            kind="document", text_content="facts")
        db.add(row); db.commit()
        identifier, owner, wid, ids = row.id, user.id, workspace.id, [item.id for item in conversations]
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0035_chat_attachments"
    barrier = Barrier(2)
    def bind(cid):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            try:
                access = auth.Access(user_id=owner, workspace_id=wid, role="admin", session_hash="test-only")
                selected = select_attachments(db, access, [identifier], ChatProviderConfig("", "", "text"))
                db.add(ChatMessage(conversation_id=cid, role="user", sequence=0, attachments=selected))
                db.commit()
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(bind, ids)) == [200, 409]
    with Session(auth_postgres) as db:
        saved = db.get(ChatAttachment, identifier)
        assert saved.message_id and saved.content_hash == "a" * 64 and saved.text_content == "facts"
        assert len(db.scalars(select(ChatMessage).where(ChatMessage.conversation_id.in_(ids))).all()) == 1
