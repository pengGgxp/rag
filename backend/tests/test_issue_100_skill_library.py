"""Issue #100 platform catalog and workspace-copy regression tests."""
pytest_plugins = ["test_resources"]

import io
import zipfile
from uuid import UUID

from sqlalchemy import select

from app import auth, workspace_files
from app.models import SiteSkill, SiteSkillFile, WorkspaceSkillInstall
from test_auth import register, share, switch
from test_shared_providers import setup_site


def platform_skill(db, user_id):
    row = SiteSkill(name="报告助手", description="生成报告", markdown="请先读取资料。", enabled=True,
                    created_by=user_id, updated_by=user_id,
                    files=[SiteSkillFile(path="scripts/report.py", content=b"print('ok')")])
    db.add(row)
    db.commit()
    return row


def access_for(client, db):
    session = db.get(auth.LoginSession, auth.digest(client.cookies.get(auth.COOKIE)))
    return auth.Access(session.user_id, session.workspace_id, "admin", session.token_hash)


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
    assert db.scalar(select(WorkspaceSkillInstall).where(WorkspaceSkillInstall.site_skill_id == skill.id)) is not None

    root = next(item for item in installed.json()["items"] if item["path"].endswith("/SKILL.md"))
    actor = access_for(owner, db)
    workspace_files._save_bytes(actor.workspace_id, actor, db, root["path"], b"user changed", "text/markdown", expected_version=1)
    repeated = owner.post(f"/agent/skills/platform/{skill.id}/install")
    assert repeated.status_code == 201 and repeated.json()["already_installed"]
    assert owner.get(f"/resources/{root['id']}/download").content == b"user changed"


def test_catalog_is_readable_to_viewers_but_install_requires_editor(accounts, monkeypatch, resource_store):
    owner, db, _ = setup_site(accounts, monkeypatch)
    skill = platform_skill(db, UUID(owner.get("/auth/me").json()["user"]["id"]))
    (_, member, _), _ = accounts
    register(member, "issue100-viewer@example.com")
    share(owner, "issue100-viewer@example.com", "viewer")
    workspace_id = owner.get("/auth/me").json()["workspace_id"]
    switch(member, workspace_id)
    assert member.get("/agent/skills/platform").status_code == 200
    assert member.get(f"/agent/skills/platform/{skill.id}/download").status_code == 200
    assert member.post(f"/agent/skills/platform/{skill.id}/install").status_code == 403
