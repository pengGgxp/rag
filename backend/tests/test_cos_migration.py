import hashlib
import pytest

from app import migrate_cos
from app.models import Document, SiteStorageSettings
from test_auth import library, register
from test_retrieval import add_document
from test_cos_uploads import FakeCos


def test_verified_migration_preserves_identity_versions_hashes_and_is_repeatable(accounts, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    kb = library(owner)
    content = b"original private content"
    from app.models import KnowledgeBase
    document = add_document(db, db.get(KnowledgeBase, kb), "evidence")
    document.size_bytes = len(content)
    document.content_hash = hashlib.sha256(content).hexdigest()
    row = SiteStorageSettings(id=1, version=2, bucket="zk-1309923787", region="ap-guangzhou", tested_version=2)
    db.add(row); db.commit()
    before = (document.id, document.object_key, document.version_group_id, document.content_hash)
    target = FakeCos()
    monkeypatch.setattr(migrate_cos, "CosStorage", lambda _: target)
    class Legacy:
        def download_fileobj(self, bucket, key, stream):
            assert key == document.object_key
            stream.write(content)
    preview = migrate_cos.migrate(db)
    assert preview["documents"] == 1 and not preview["active"] and not target.objects
    with pytest.raises(ValueError, match="版本"):
        migrate_cos.migrate(db, execute=True, version=1, legacy=Legacy())
    db.rollback()
    result = migrate_cos.migrate(db, execute=True, version=2, legacy=Legacy())
    assert result["active"] and result["verified"]
    assert target.objects[document.object_key] == content
    assert (document.id, document.object_key, document.version_group_id, document.content_hash) == before
    assert migrate_cos.migrate(db, execute=True, version=2, legacy=Legacy())["verified"]
    assert target.downloads == 1


def test_bad_source_file_never_activates_target(accounts, monkeypatch):
    (owner, _, _), db = accounts
    register(owner)
    from app.models import KnowledgeBase
    doc = add_document(db, db.get(KnowledgeBase, library(owner)), "evidence")
    db.add(SiteStorageSettings(id=1, version=1, bucket="zk-1309923787", region="ap-guangzhou", tested_version=1))
    db.commit()
    monkeypatch.setattr(migrate_cos, "CosStorage", lambda _: FakeCos())
    class BadSource:
        def download_fileobj(self, bucket, key, stream):
            stream.write(b"corrupted")
    with pytest.raises(ValueError, match="摘要或大小"):
        migrate_cos.migrate(db, execute=True, version=1, legacy=BadSource())
    db.rollback()
    assert not db.get(SiteStorageSettings, 1).active
