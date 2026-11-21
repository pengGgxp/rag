import hashlib
from tempfile import TemporaryFile

import pytest
from fastapi import HTTPException, Request, UploadFile
from sqlalchemy import func, select
from starlette.datastructures import Headers

from app import auth, main
from app.models import Document, IngestionJob, KnowledgeBase, LoginSession
from test_auth import library, register
from test_retrieval import add_document


@pytest.mark.parametrize("new_version", [False, True])
@pytest.mark.parametrize("size", [0, 200 * 1024 * 1024, 200 * 1024 * 1024 + 1])
def test_real_spooled_upload_boundary_before_storage_and_jobs(accounts, monkeypatch, size, new_version):
    (owner, _, _), db = accounts
    register(owner)
    kb_id = library(owner)
    previous = add_document(db, db.get(KnowledgeBase, kb_id), "previous version") if new_version else None
    session = db.get(LoginSession, auth.digest(owner.cookies.get(auth.COOKIE)))
    access = auth.get_access(session)
    stored, queued = [], []

    def store(fileobj, **kwargs):
        assert fileobj.tell() == 0
        total = 0
        digest = hashlib.sha256()
        while block := fileobj.read(1024 * 1024):
            total += len(block)
            digest.update(block)
        stored.append((total, digest.hexdigest()))

    monkeypatch.setattr(main, "upload_fileobj", store)
    monkeypatch.setattr(main.process_document, "delay", queued.append)
    before_docs = db.scalar(select(func.count()).select_from(Document))
    before_jobs = db.scalar(select(func.count()).select_from(IngestionJob))
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    with TemporaryFile() as stream:
        stream.truncate(size)
        upload = UploadFile(stream, filename="large.txt", headers=Headers({"content-type": "text/plain"}))
        if size == main.MAX_DOCUMENT_BYTES:
            result = main.upload_document(kb_id, request, upload, previous.id if previous else None, db, access)
            assert result.size_bytes == size
            assert stored == [(size, db.get(Document, result.id).content_hash)]
            assert len(queued) == 1
            assert result.version_number == (2 if new_version else 1)
            if previous:
                assert result.supersedes_id == previous.id
                assert previous.is_active
        else:
            with pytest.raises(HTTPException) as caught:
                main.upload_document(kb_id, request, upload, previous.id if previous else None, db, access)
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

    assert main.inspect_upload(Stream()) == (12, hashlib.sha256(b"text" * 3).hexdigest())
