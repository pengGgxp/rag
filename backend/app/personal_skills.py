"""Skill import is an ordinary workspace resource upload, with no published copy."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from . import skill_files
from .auth import get_access
from .audit_log import record_audit
from .db import get_db
from .models import WorkspaceFile
from .workspace_files import _stage_bytes, owned as resource_owned, read_file

router = APIRouter(prefix="/agent/skills", tags=["workspace-skills"])


# These helpers also serve personal MCP connections; their ownership is unchanged.
def owned(db, model, identifier, access, lock=False):
    query = select(model).where(model.id == identifier, model.owner_id == access.user_id,
        model.workspace_id == access.workspace_id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "扩展不存在或无权访问")
    return row


def audit(db, access, request, action, row):
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action=action, resource_type="mcp_server", resource_id=row.id,
        metadata={"version": row.version}, request=request)


def save_package(files, db, access):
    """Use the same keys, quota, audit and metadata writer as ordinary resources."""
    access.editor()
    skill_files.validate_files(files)
    # A fresh directory avoids overwriting user files, including concurrent imports.
    directory = f"skills/{uuid4().hex}"
    uploaded, rows = [], []
    try:
        from .storage_billing import check_capacity
        from .shared_resources import object_key
        check_capacity(db, access.workspace_id, sum(map(len, files.values())))
        with db.begin_nested():
            for path, content in files.items():
                uploaded.append(object_key(access.workspace_id, f"{directory}/{path}"))
                rows.append(_stage_bytes(access.workspace_id, access, db,
                    f"{directory}/{path}", content, "application/octet-stream", None))
                rows[-1].source = "resource_upload"
            db.flush()
        db.commit()
    except BaseException:
        db.rollback()
        from .storage import delete_object
        for key in uploaded:
            try:
                delete_object(key)
            except Exception:
                pass
        raise
    return {"directory": directory, "items": [read_file(row) for row in rows]}


@router.get("")
def listing(db=Depends(get_db), access=Depends(get_access)):
    from .shared_resources import refresh_index
    refresh_index(db, access)
    return [read_file(row) for row in db.scalars(select(WorkspaceFile).where(
        WorkspaceFile.workspace_id == access.workspace_id, WorkspaceFile.deleted_at.is_(None),
        (WorkspaceFile.path == "SKILL.md") | WorkspaceFile.path.endswith("/SKILL.md"))
        .order_by(WorkspaceFile.path))]


@router.post("/import", status_code=201)
def importing(file: UploadFile = File(...), db=Depends(get_db), access=Depends(get_access)):
    access.editor()
    name = (file.filename or "").lower()
    if not name.endswith((".md", ".zip")):
        raise HTTPException(422, "请选择 SKILL.md 或 ZIP 格式的 Skill 包")
    data = file.file.read(skill_files.MAX_UPLOAD_BYTES + 1)
    if len(data) > skill_files.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Skill 包不能超过 25 MiB")
    files = {"SKILL.md": data} if name.endswith(".md") else skill_files.unzip(data)[0]
    return save_package(files, db, access)


@router.post("/import-folder", status_code=201)
def importing_folder(files: list[UploadFile] = File(...), paths: list[str] = Form(...),
                           db=Depends(get_db), access=Depends(get_access)):
    access.editor()
    if len(files) != len(paths) or len(files) > skill_files.MAX_FILES:
        raise HTTPException(422, "请选择包含 SKILL.md 的单个文件夹")
    entries, total = [], 0
    for file, path in zip(files, paths):
        data = file.file.read(skill_files.MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > skill_files.MAX_FILE_BYTES or total > skill_files.MAX_BUNDLE_BYTES:
            raise HTTPException(413, "Skill 文件超过大小限制")
        entries.append((path, data))
    return save_package(skill_files.imported_files(entries)[0], db, access)


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = resource_owned(db, access, access.workspace_id, identifier)
    from .shared_resources import refresh_current_metadata
    return read_file(refresh_current_metadata(db, access, row))
