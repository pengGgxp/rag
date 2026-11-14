"""Skill import is an ordinary workspace resource upload, with no published copy."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import skill_files
from .auth import get_access
from .audit_log import record_audit
from .db import get_db
from .models import WorkspaceFile, WorkspaceFileVersion
from .workspace_files import owned as resource_owned, read_file

router = APIRouter(prefix="/agent/skills", tags=["workspace-skills"])


class SkillContentUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=5_000_000)
    version: int = Field(default=1, ge=1, strict=True)
    revision: str | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


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
    from .skill_discovery import parse, directory_name
    from .workspace_mutations import save_files, snapshot, unique_name
    from .file_operations import workspace_lock
    import mimetypes
    access.editor()
    skill_files.validate_files(files)
    metadata = parse(files["SKILL.md"].decode("utf-8"), "SKILL.md")
    directory = ".skills/" + directory_name(metadata.get("name") or "skill")
    with workspace_lock(db, access.workspace_id):
        current, dirs, _ = snapshot(db, access)
        if directory in current or directory in dirs:
            directory = unique_name(directory, set(current) | dirs)
        result = save_files(db, access, {f"{directory}/{path}": (content,
            mimetypes.guess_type(path)[0] or "application/octet-stream") for path, content in files.items()})
        return {"directory": directory, **result}


@router.get("")
def listing(db=Depends(get_db), access=Depends(get_access)):
    from .skill_discovery import discover
    return discover(db, access)


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
    row = refresh_current_metadata(db, access, row)
    if not row.path.lower().endswith("/skill.md") and row.path.lower() != "skill.md":
        raise HTTPException(422, "请使用 Skill 入口打开 SKILL.md")
    value = read_file(row)
    try:
        version = db.scalar(select(WorkspaceFileVersion).where(
            WorkspaceFileVersion.file_id == row.id, WorkspaceFileVersion.version == row.current_version,
            WorkspaceFileVersion.deleted_at.is_(None)))
        if version is None:
            raise HTTPException(404, "Skill 文件已删除")
        from .shared_resources import backend
        from .skill_discovery import MAX_MANIFEST_BYTES
        with backend(db).download(version.object_key) as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            value.update(name=None, description="", manifest_path=row.path,
                directory=row.path.rsplit("/", 1)[0], manifest_error="清单超过 5 MB，请在工作区替换文件后重试")
            return value
        content = raw.decode("utf-8")
        from .skill_discovery import parse
        value.update(**parse(content, row.path), markdown=content)
    except UnicodeDecodeError:
        value.update(name=None, description="", manifest_path=row.path,
            directory=row.path.rsplit("/", 1)[0], manifest_error="清单需要 UTF-8 文本，请在工作区替换文件后重试")
    return value


@router.put("/{identifier}")
def update(identifier: UUID, payload: SkillContentUpdate, request: Request,
           db=Depends(get_db), access=Depends(get_access)):
    row = resource_owned(db, access, access.workspace_id, identifier, write=True)
    if not row.path.lower().endswith("/skill.md") and row.path.lower() != "skill.md":
        raise HTTPException(422, "请使用 Skill 入口修改 SKILL.md")
    from .workspace_mutations import save_files
    result = save_files(db, access, {row.path: (payload.content.encode("utf-8"), "text/markdown")},
        expected={row.path: payload.revision} if payload.revision else {}, request_id=payload.request_id)["items"][0]
    return {**result, "markdown": payload.content, "directory": row.path.rsplit("/", 1)[0] if "/" in row.path else ""}


@router.get("/{identifier}/files")
def files(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = resource_owned(db, access, access.workspace_id, identifier)
    directory = row.path.rsplit("/", 1)[0] if "/" in row.path else ""
    query = select(WorkspaceFile).where(WorkspaceFile.workspace_id == access.workspace_id,
        WorkspaceFile.deleted_at.is_(None), WorkspaceFile.path.startswith(directory + "/"))
    return [read_file(item) for item in db.scalars(query.order_by(WorkspaceFile.path))]


@router.delete("/{identifier}", status_code=204)
def remove(identifier: UUID, request: Request, confirmation: str | None = None,
           request_id: str | None = None, db=Depends(get_db), access=Depends(get_access)):
    row = resource_owned(db, access, access.workspace_id, identifier, write=True)
    from .workspace_mutations import delete
    directory = row.path.rsplit("/", 1)[0] if "/" in row.path else ""
    delete(db, access, [directory or row.path], confirmation=confirmation, request_id=request_id)
