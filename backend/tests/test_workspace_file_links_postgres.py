"""指定路径的并发补索引仅创建一个资源；使用隔离 PostgreSQL。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, shared_resources
from app.models import WorkspaceFile, WorkspaceFileVersion, LoginSession
from app.workspace_file_links import get_file_link
from test_auth_postgres import auth_postgres
from test_cloud_computer_mount import MemoryMount


def test_concurrent_exact_link_index_keeps_one_identity(auth_postgres, monkeypatch):
    store = MemoryMount()
    monkeypatch.setattr(shared_resources, "backend", lambda _: store)
    with Session(auth_postgres) as db:
        user = auth.create_user(db, f"link-{uuid4().hex}@example.com", "文件链接并发验收")
        workspace = user.id
        token = uuid4().hex
        db.add(LoginSession(token_hash=token, user_id=user.id,
            csrf_token=uuid4().hex, expires_at=auth.now() + timedelta(hours=1)))
        db.commit()
        access = auth.Access(user.id, token)
    store.objects[shared_resources.object_key(workspace, "成果/命令生成.pptx")] = b"command-created"
    barrier = Barrier(4)

    def resolve(_):
        with Session(auth_postgres) as db:
            barrier.wait(timeout=10)
            return get_file_link(db, access, {"path": "成果/命令生成.pptx"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolve, range(4)))
    assert len({item["id"] for item in results}) == 1
    assert len({item["url"] for item in results}) == 1
    with Session(auth_postgres) as db:
        rows = list(db.scalars(select(WorkspaceFile).where(WorkspaceFile.account_id == workspace)))
        assert len(rows) == 1
        assert len(list(db.scalars(select(WorkspaceFileVersion).where(
            WorkspaceFileVersion.file_id == rows[0].id)))) == 1
