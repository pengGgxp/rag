import hashlib
import io
from datetime import timedelta
from uuid import UUID

import pytest
from PIL import Image

from app.identity import auth
from app.models import WorkspaceFile, WorkspaceFileVersion
from app.shared_resources import object_key
from test_auth import register
from test_cloud_computer_mount import MemoryMount


@pytest.fixture
def store(monkeypatch):
    storage = MemoryMount()
    monkeypatch.setattr("app.file_preview_access.current_storage", lambda *_args, **_kwargs: storage)
    monkeypatch.setattr("app.shared_resources.backend", lambda _db: storage)
    monkeypatch.setattr("app.storage_downloads.download_range_fileobj",
                        lambda key, start, end: io.BytesIO(storage.objects[key][start:end + 1]))
    return storage


def resource(db, client, storage, path="photo.png", data=None):
    identity = register(client)
    if data is None:
        stream = io.BytesIO()
        Image.new("RGB", (800, 600), "#0a9a8f").save(stream, "PNG")
        data = stream.getvalue()
    user_id = UUID(identity["user"]["id"])
    partition = user_id
    row = WorkspaceFile(account_id=partition, path=path,
        content_type="image/png" if path.endswith(".png") else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data), content_hash=hashlib.sha256(data).hexdigest(), current_version=1,
        source="resource_upload", created_by=user_id, updated_by=user_id,
        expires_at=auth.now() + timedelta(days=365))
    key = object_key(partition, path)
    row.versions.append(WorkspaceFileVersion(account_id=partition, version=1, object_key=key,
        content_type=row.content_type, size_bytes=row.size_bytes, content_hash=row.content_hash, created_by=user_id))
    db.add(row); db.commit()
    storage.objects[key] = data
    return row, identity
