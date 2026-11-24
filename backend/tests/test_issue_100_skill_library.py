"""Issue #100 platform catalog and workspace-copy regression tests."""
pytest_plugins = ["test_resources"]

import io
import zipfile
from uuid import UUID

from sqlalchemy import select

from app import workspace_files

from app.identity import auth
from app.models import SiteSkill, SiteSkillFile, WorkspaceSkillInstall
from test_auth import register, share, switch
from test_shared_providers import setup_site
from test_resources import resource_store


def platform_skill(db, user_id):
    row = SiteSkill(name="报告助手", description="生成报告", markdown="请先读取资料。", enabled=True,
                    created_by=user_id, updated_by=user_id,
                    files=[SiteSkillFile(path="scripts/report.py", content=b"print('ok')")])
    db.add(row)
    db.commit()
    return row


def access_for(client, db):
    session = db.get(auth.LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
    return auth.get_access(session)


def test_catalog_download_install_is_idempotent_and_keeps_workspace_copy(accounts, monkeypatch, resource_store):
    owner, db, _ = setup_site(accounts, monkeypatch)
    skill = platform_skill(db, UUID(owner.get("/auth/me").json()["user"]["id"]))

    catalog = owner.get("/agent/skills/platform?q=报告")
    assert catalog.status_code == 200 and catalog.json()[0]["id"] == str(skill.id)
    detail = owner.get(f"/agent/skills/platform/{skill.id}")
    assert detail.status_code == 200 and detail.json()["markdown"] == skill.markdown
    archive = owner.get(f"/agent/skills/platform/{skill.id}/download")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        assert package.read("scripts/report.py") == b"print('ok')"

    installed = owner.post(f"/agent/skills/platform/{skill.id}/install")
    assert installed.status_code == 201 and not installed.json()["already_installed"]
    directory = installed.json()["directory"]
    again = owner.post(f"/agent/skills/platform/{skill.id}/install")
    assert again.status_code == 201 and again.json()["already_installed"] and again.json()["directory"] == directory
    assert owner.get("/resources").json()["items"]
    assert db.scalar(select(WorkspaceSkillInstall).where(WorkspaceSkillInstall.site_skill_id == skill.id)) is None

    root = next(item for item in installed.json()["items"] if item["path"].endswith("/SKILL.md"))
    actor = access_for(owner, db)
    changed = "---\nname: 报告助手\n---\nuser changed".encode()
    workspace_files._save_bytes(actor.user_id, actor, db, root["path"], changed, "text/markdown", expected_version=1)
    repeated = owner.post(f"/agent/skills/platform/{skill.id}/install")
    assert repeated.status_code == 201 and repeated.json()["already_installed"]
    assert owner.get(f"/resources/{root['id']}/download").content == changed


def test_accounts_can_install_platform_skills_independently(accounts, monkeypatch, resource_store):
    owner, db, _ = setup_site(accounts, monkeypatch)
    skill = platform_skill(db, UUID(owner.get("/auth/me").json()["user"]["id"]))
    (_, member, _), _ = accounts
    register(member, "issue100-viewer@example.com")
    assert member.get("/agent/skills/platform").status_code == 200
    assert member.get(f"/agent/skills/platform/{skill.id}/download").status_code == 200
    assert member.post(f"/agent/skills/platform/{skill.id}/install").status_code == 201
    assert owner.get("/agent/skills").json() == []


def test_workspace_browser_keeps_directories_and_file_operations_in_one_mount(accounts, monkeypatch, resource_store):
    owner, db, _ = setup_site(accounts, monkeypatch)
    created = owner.post("/resources/directories", json={"path": "docs"})
    assert created.status_code == 201
    assert owner.post("/resources/directories", json={"path": "docs/nested"}).status_code == 201
    upload = owner.post("/resources", data={"directory": "docs/nested"}, files={"file": ("guide.txt", b"guide")})
    assert upload.status_code == 201, upload.text
    item = upload.json()
    listing = owner.get("/resources/browse", params={"path": "", "q": "guide"})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["path"] == "docs/nested/guide.txt"
    assert owner.put(f"/resources/files/{item['id']}", json={"path": item["path"], "new_path": "docs/nested/readme.txt"}).status_code == 200
    assert owner.post("/resources/operations/copy", json={"paths": ["docs/nested"], "destination": ""}).status_code == 200
    copied = owner.get("/resources/browse", params={"path": "docs", "q": ""}).json()["items"]
    assert {entry["path"] for entry in copied} == {"docs/nested"}
    assert owner.post("/resources/operations/move", json={"paths": ["docs/nested/readme.txt"], "destination": ""}).status_code == 200
    assert owner.request("DELETE", "/resources/directories", json={"path": "docs"}).status_code == 204
    assert owner.get("/resources/browse").json()["items"]
