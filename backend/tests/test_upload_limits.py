import hashlib
from tempfile import TemporaryFile

import pytest
from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import func, select
from starlette.datastructures import Headers

from app import auth
from app.knowledge import routes as knowledge_routes
from app.models import Document, IngestionJob, KnowledgeBase, LoginSession
from test_auth import library, register
from test_retrieval import add_document


@pytest.mark.parametrize("new_version", [False, True])
@pytest.mark.parametrize("size", [0, 200 * 1024 * 1024, 200 * 1024 * 1024 + 1])
def test_real_spooled_upload_boundary_before_storage_and_jobs(accounts, monkeypatch, tmp_path, size, new_version):
    (owner, _, _), db = accounts
    register(owner)
    kb_id = library(owner)
    previous = add_document(db, db.get(KnowledgeBase, kb_id), "previous version") if new_version else None
    session = db.get(LoginSession, auth.digest(owner.cookies.get(auth.COOKIE)))
    access = auth.get_access(session)
    stored, queued = [], []
    from shutil import copyfile, copyfileobj

    class DiskStore:
        def path(self, key):
            return tmp_path / hashlib.sha256(key.encode()).hexdigest()

        def put(self, key, fileobj, _):
            with self.path(key).open("wb") as target:
                copyfileobj(fileobj, target)
            stored.append(self.path(key).stat().st_size)

        def head(self, key):
            path = self.path(key)
            return {"Content-Length": path.stat().st_size} if path.exists() else None

        def download(self, key):
            return self.path(key).open("rb")

        def copy(self, source, target, **_):
            copyfile(self.path(source), self.path(target))

        def delete(self, key):
            self.path(key).unlink(missing_ok=True)

    storage = DiskStore()

    monkeypatch.setattr("app.file_operations.storage", lambda _: storage)
    monkeypatch.setattr(knowledge_routes.process_document, "delay", queued.append)
    if previous:
        storage.path(previous.object_key).write_bytes(b"previous version")
    kwargs = {"replace_document_id": previous.id if previous else None,
              "expected_content_hash": previous.content_hash if previous else None, "db": db, "access": access}
    before_docs = db.scalar(select(func.count()).select_from(Document))
    before_jobs = db.scalar(select(func.count()).select_from(IngestionJob))
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    with TemporaryFile() as stream:
        stream.truncate(size)
        upload = UploadFile(stream, filename="large.txt", headers=Headers({"content-type": "text/plain"}))
        if size == knowledge_routes.MAX_DOCUMENT_BYTES:
            result = knowledge_routes.upload_document(kb_id, request, upload, **kwargs)
            assert result.size_bytes == size
            assert stored == [size]
            assert len(queued) == 1
            assert result.version_number == 1
            if previous:
                assert result.id == previous.id
                assert previous.is_active
        else:
            with pytest.raises(HTTPException) as caught:
                knowledge_routes.upload_document(kb_id, request, upload, **kwargs)
            assert caught.value.status_code == (400 if size == 0 else 413)
            assert stored == queued == []
            assert db.scalar(select(func.count()).select_from(Document)) == before_docs
            assert db.scalar(select(func.count()).select_from(IngestionJob)) == before_jobs


def test_upload_inspection_only_uses_bounded_reads():
    class Stream:
        def __init__(self):
            self.remaining = 3

        def seek(self, offset):
            assert offset == 0

        def read(self, count):
            assert count <= 1024 * 1024
            self.remaining -= 1
            return b"text" if self.remaining >= 0 else b""

    from app.knowledge.ingestion.admission import inspect_file
    assert inspect_file(Stream()) == (12, hashlib.sha256(b"text" * 3).hexdigest())
