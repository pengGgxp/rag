import hashlib
import io
from uuid import uuid4

from app import skill_uploads
from app.models import SiteSkill, SkillUpload, WorkspaceFile
from test_shared_providers import setup_site
from test_skill_resources import zipped
from test_resources import resource_store


class MultipartStore:
    def __init__(self):
        self.objects = {}
        self.pending = {}

    def begin(self, key, _content_type):
        identifier = str(uuid4())
        self.pending[(key, identifier)] = {}
        return identifier

    def part_url(self, key, identifier, number, _size):
        return f"memory://{key}/{identifier}/{number}"

    def parts(self, key, identifier):
        return [{"PartNumber": number, "Size": len(body), "ETag": hashlib.md5(body).hexdigest()}
            for number, body in sorted(self.pending[(key, identifier)].items())]

    def upload_part(self, key, identifier, number, body):
        self.pending[(key, identifier)][number] = body

    def complete(self, key, identifier, _parts):
        parts = self.pending.pop((key, identifier))
        self.objects[key] = b"".join(parts[number] for number in sorted(parts))

    def download(self, key):
        return io.BytesIO(self.objects[key])

    def head(self, key):
        body = self.objects.get(key)
        return {"Content-Length": len(body)} if body is not None else None

    def delete(self, key):
        self.objects.pop(key, None)

    def abort(self, key, identifier):
        self.pending.pop((key, identifier), None)


def test_direct_upload_previews_and_imports_selected_skill_only(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    store = MultipartStore()
    monkeypatch.setattr(skill_uploads, "current_storage", lambda _db: store)
    archive = zipped({
        "set/one/SKILL.md": b"---\nname: one\n---\nOne full instruction body",
        "set/one/a.txt": b"resource one",
        "set/two/SKILL.md": b"---\nname: two\n---\nTwo full instruction body",
    })
    started = client.post("/agent/skills/uploads", json={"filename": "skills.zip", "size_bytes": len(archive)})
    assert started.status_code == 201, started.text
    upload_id = started.json()["id"]
    from uuid import UUID
    row = db.get(SkillUpload, UUID(upload_id))
    signed = client.post(f"/agent/skills/uploads/{upload_id}/parts/1", json={})
    assert signed.status_code == 200
    store.upload_part(row.object_key, row.multipart_id, 1, archive)
    assert client.post(f"/agent/skills/uploads/{upload_id}/complete", json={}).status_code == 200

    preview = client.get(f"/agent/skills/uploads/{upload_id}/preview")
    assert preview.status_code == 200, preview.text
    items = preview.json()["items"]
    assert [item["name"] for item in items] == ["one", "two"]
    assert "One full instruction body" in items[0]["markdown"]
    assert items[0]["resources"][0]["path"] == "a.txt"
    imported = client.post(f"/agent/skills/uploads/{upload_id}/import", json={"items": [1]})
    assert imported.status_code == 201, imported.text
    saved = imported.json()["items"]
    assert len(saved) == 1
    assert saved[0]["directory"].endswith("two")
    assert db.get(SkillUpload, UUID(upload_id)).state == "cleaned"
    paths = {row.path for row in db.query(WorkspaceFile).all()}
    assert any(path.endswith("/SKILL.md") for path in paths)


def test_direct_upload_is_bound_to_user_and_workspace(accounts, monkeypatch, resource_store):
    client, _, _ = setup_site(accounts, monkeypatch)
    store = MultipartStore()
    monkeypatch.setattr(skill_uploads, "current_storage", lambda _db: store)
    started = client.post("/agent/skills/uploads", json={"filename": "skill.md", "size_bytes": 4})
    assert started.status_code == 201
    identifier = started.json()["id"]
    _, outsider, _ = accounts[0]
    response = outsider.post(f"/agent/skills/uploads/{identifier}/parts/1", json={})
    assert response.status_code in (401, 404)


def test_site_upload_import_creates_separate_disabled_skills(accounts, monkeypatch, resource_store):
    client, db, _ = setup_site(accounts, monkeypatch)
    store = MultipartStore()
    monkeypatch.setattr(skill_uploads, "current_storage", lambda _db: store)
    archive = zipped({
        "first/SKILL.md": b"---\nname: first\n---\nFirst body",
        "second/SKILL.md": b"---\nname: second\n---\nSecond body",
    })
    started = client.post("/site/admin/skills/uploads", json={
        "filename": "collection.zip", "size_bytes": len(archive)})
    assert started.status_code == 201, started.text
    upload_id = started.json()["id"]
    from uuid import UUID
    row = db.get(SkillUpload, UUID(upload_id))
    store.upload_part(row.object_key, row.multipart_id, 1, archive)
    assert client.post(f"/site/admin/skills/uploads/{upload_id}/complete", json={}).status_code == 200
    preview = client.get(f"/site/admin/skills/uploads/{upload_id}/preview")
    assert [item["name"] for item in preview.json()["items"]] == ["first", "second"]
    imported = client.post(f"/site/admin/skills/uploads/{upload_id}/import", json={"items": [0, 1]})
    assert imported.status_code == 201, imported.text
    assert [row.name for row in db.query(SiteSkill).order_by(SiteSkill.name)] == ["first", "second"]
    assert all(not row.enabled for row in db.query(SiteSkill).all())
