"""Temporary local browser fixtures, never imported by production."""
import os
from uuid import uuid4

from app import auth
from app.db import SessionLocal
from app.models import SiteSkill, SiteSkillFile
from workspace_local_preview import LocalFiles, LocalS3


def main():
    if os.environ.get("ISSUE100_LOCAL_PREVIEW") != "1":
        raise RuntimeError("local preview only")
    from sqlalchemy.engine import make_url
    from app.config import get_settings
    if not make_url(get_settings().database_url).database.startswith("rag_auth_test_issue100_browser_"):
        raise RuntimeError("refusing non-disposable database")
    store = LocalFiles() if os.environ.get("ISSUE100_LOCAL_STORE") == "filesystem" else LocalS3()
    from app import shared_resources as shared
    shared.backend = lambda db: store
    with SessionLocal() as db:
        owner = auth.create_user(db, "issue100-local@example.com", "临时验收", "local-preview-password-100")
        workspace = owner.id
        owner.email_verified_at = auth.now()
        viewer = auth.create_user(db, "issue100-viewer@example.com", "只读验收", "local-preview-password-100")
        other_workspace = viewer.id
        viewer.email_verified_at = auth.now()
        skill = SiteSkill(name="报告助手", description="整理工作区资料并生成报告", markdown="按需读取 references/guide.md。", enabled=True,
            created_by=owner.id, updated_by=owner.id, files=[SiteSkillFile(path="scripts/report.py", content=b"print('local-only')"),
                SiteSkillFile(path="references/guide.md", content="本地临时参考资料".encode())])
        db.add(skill); db.commit()
        actor = auth.Access(owner.id, "")
        from app.workspace_mutations import save_files, create_directory
        create_directory(db, actor, "项目资料")
        create_directory(db, actor, "项目资料/目标文件夹")
        save_files(db, actor, {
            "项目资料/会议纪要.md": ("# 会议纪要\n\n这是隔离本地验收文件。\n".encode(), "text/markdown"),
            "项目资料/用于检查超长名称在卡片列表以及手机中的显示与焦点的临时文件名称.txt": (b"local-only", "text/plain"),
            "项目资料/run.py": (b"print('local')", "text/x-python"),
        })
        # A large real object listing exercises pagination beyond the old 500 cap.
        for index in range(505):
            store.put(shared.object_key(workspace, f"分页验收/item-{index:03}.txt"), __import__("io").BytesIO(b"local"), "text/plain")
        print("LOCAL_BROWSER_FIXTURES_READY")


if __name__ == "__main__":
    main()
