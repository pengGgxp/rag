"""Concurrent personal edits use the same locking boundary as permission checks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from app.integrations import receipts
from app.identity import auth
from app.models import ChatMessage, LoginSession, MCPServer, RemoteToolExecution
from test_auth_postgres import auth_postgres
from test_platform_skills_postgres import actor, call


def test_mcp_versions_are_atomic(auth_postgres):
    raw, csrf, _ = actor(auth_postgres)
    mcp = {"name": "Remote", "url": "https://mcp.example.com/mcp"}
    remote = call(raw, csrf, "POST", "/agent/mcp", mcp)
    assert remote.status_code == 201, remote.text
    for path, payload in (
        (f"/agent/mcp/{remote.json()['id']}", {**mcp, "version": 1}),
    ):
        barrier = Barrier(2)
        def update(_):
            barrier.wait(timeout=10)
            return call(raw, csrf, "PUT", path, payload).status_code
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(update, range(2))) == [200, 409]
    with Session(auth_postgres) as db:
        assert db.get(MCPServer, UUID(remote.json()["id"])).version == 2


def test_remote_invocation_concurrent_claim_replay_and_uncertain_write(auth_postgres):
    raw, csrf, user_id = actor(auth_postgres)
    conversation = call(raw, csrf, "POST", "/conversations", {}).json()
    factory = sessionmaker(bind=auth_postgres, expire_on_commit=False)
    request, identifier = uuid4(), uuid4()
    with factory() as db:
        session = db.get(LoginSession, auth.digest(raw))
        access = auth.get_access(session)
        message = ChatMessage(conversation_id=UUID(conversation["id"]), request_id=request,
            sequence=1, role="assistant", content="")
        db.add(message); db.commit()
        message_id = message.id
    barrier = Barrier(4)
    def claim(_):
        barrier.wait(timeout=10)
        try:
            return receipts.claim(factory, access, message_id, request, identifier, "mcp_test", "args", 1)
        except receipts.ReceiptError as error:
            return error
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(claim, range(4)))
    admitted = [value for value in results if isinstance(value, tuple)]
    assert len(admitted) == 1
    assert all(value.uncertain for value in results if isinstance(value, receipts.ReceiptError))
    execution, _ = admitted[0]
    receipts.finish(factory, execution, {"saved": True})
    assert receipts.claim(factory, access, message_id, request, identifier, "mcp_test", "args", 1)[1] == {"saved": True}
    import pytest
    with pytest.raises(receipts.ReceiptError, match="冲突"):
        receipts.claim(factory, access, message_id, request, identifier, "mcp_test", "changed", 1)
    pending, _ = receipts.claim(factory, access, message_id, request, uuid4(), "mcp_write", "args", 1)
    receipts.finish(factory, pending, status="uncertain")
    with pytest.raises(receipts.ReceiptError, match="尚未确认"):
        receipts.claim(factory, access, message_id, request, uuid4(), "mcp_write", "args", 1)
    assert receipts.claim(factory, access, message_id, request, uuid4(), "mcp_write", "args", 1, readonly=True)[1] is None
    with factory() as db:
        assert len(db.scalars(select(RemoteToolExecution).where(RemoteToolExecution.message_id == message_id)).all()) == 3
