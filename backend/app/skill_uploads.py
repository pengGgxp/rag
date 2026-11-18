"""Temporary, scoped direct-to-object-storage uploads for Skill packages."""
import math
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .auth import Access, aware, get_access, now
from .db import SessionLocal, get_db
from .models import SkillUpload
from .site_auth import site_admin
from .storage_backend import current_storage

router = APIRouter(tags=["skill-uploads"])
PART_SIZE = 8 * 1024 * 1024


class UploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=200 * 1024 * 1024, strict=True)


class ImportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[int] = Field(min_length=1)


def view(row, parts=None):
    return {"id": str(row.id), "filename": row.filename, "size_bytes": row.size_bytes,
        "part_size": PART_SIZE, "state": row.state, "expires_at": row.expires_at.isoformat(),
        "parts": parts or []}


def start(payload, db, user_id, workspace_id, scope):
    suffix = Path(payload.filename).suffix.lower()
    if suffix not in (".zip", ".md"):
        raise HTTPException(422, "请选择 SKILL.md 或 ZIP 格式的 Skill 包")
    identifier = uuid4()
    key = f"_skill_uploads/{identifier}{suffix}"
    storage = current_storage(db)
    multipart_id = storage.begin(key, "application/zip" if suffix == ".zip" else "text/markdown")
    row = SkillUpload(id=identifier, user_id=user_id, workspace_id=workspace_id, scope=scope,
        filename=payload.filename, size_bytes=payload.size_bytes, object_key=key,
        multipart_id=multipart_id, state="uploading", expires_at=now() + timedelta(hours=24))
    db.add(row)
    try:
        db.commit()
    except Exception:
        storage.abort(key, multipart_id)
        raise
    return view(row)


def owned(db, identifier, user_id, workspace_id, scope):
    row = db.get(SkillUpload, identifier, with_for_update=True, populate_existing=True)
    if not row or row.user_id != user_id or row.scope != scope or row.workspace_id != workspace_id:
        raise HTTPException(404, "上传任务不存在")
    if aware(row.expires_at) <= now() and row.state in ("uploading", "completing", "completed"):
        raise HTTPException(410, "上传已过期，请重新选择文件")
    return row


def parts(storage, row):
    result = storage.parts(row.object_key, row.multipart_id)
    if isinstance(result, dict):
        result = [result]
    return sorted(result, key=lambda part: int(part["PartNumber"]))


@router.post("/agent/skills/uploads", status_code=201)
def create_personal(payload: UploadInput, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor()
    return start(payload, db, access.user_id, access.workspace_id, "workspace")


@router.post("/site/admin/skills/uploads", status_code=201)
def create_site(payload: UploadInput, db=Depends(get_db), actor=Depends(site_admin)):
    return start(payload, db, actor.id, None, "site")


@router.post("/agent/skills/uploads/{identifier}/parts/{number}")
def sign_personal(identifier: UUID, number: int, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor()
    return sign(db, identifier, number, access.user_id, access.workspace_id, "workspace")


@router.post("/site/admin/skills/uploads/{identifier}/parts/{number}")
def sign_site(identifier: UUID, number: int, db=Depends(get_db), actor=Depends(site_admin)):
    return sign(db, identifier, number, actor.id, None, "site")


def sign(db, identifier, number, user_id, workspace_id, scope):
    row = owned(db, identifier, user_id, workspace_id, scope)
    if row.state != "uploading" or number < 1 or number > math.ceil(row.size_bytes / PART_SIZE):
        raise HTTPException(409, "此上传任务不能继续传输")
    size = min(PART_SIZE, row.size_bytes - (number - 1) * PART_SIZE)
    return {"url": current_storage(db).part_url(row.object_key, row.multipart_id, number, size),
        "expires_in": 900, "size": size}


@router.post("/agent/skills/uploads/{identifier}/complete")
def complete_personal(identifier: UUID, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor()
    return complete(db, identifier, access.user_id, access.workspace_id, "workspace")


@router.post("/site/admin/skills/uploads/{identifier}/complete")
def complete_site(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    return complete(db, identifier, actor.id, None, "site")


def complete(db, identifier, user_id, workspace_id, scope):
    row = owned(db, identifier, user_id, workspace_id, scope)
    if row.state == "completed":
        return view(row)
    if row.state not in ("uploading", "completing"):
        raise HTTPException(409, "此上传任务已经结束，请重新上传")
    storage = current_storage(db)
    head = storage.head(row.object_key)
    if row.state == "uploading":
        uploaded = parts(storage, row)
        expected_count = math.ceil(row.size_bytes / PART_SIZE)
        if len(uploaded) != expected_count:
            raise HTTPException(409, "仍有分片未上传完成")
        for index, part in enumerate(uploaded, 1):
            size = min(PART_SIZE, row.size_bytes - (index - 1) * PART_SIZE)
            if int(part["PartNumber"]) != index or int(part["Size"]) != size:
                raise HTTPException(409, "分片校验失败，请重新上传")
        row.state = "completing"
        db.commit()
    if head is None:
        uploaded = parts(storage, row)
        storage.complete(row.object_key, row.multipart_id,
            [{"PartNumber": int(part["PartNumber"]), "ETag": part["ETag"]} for part in uploaded])
        head = storage.head(row.object_key)
    if not head or int(head.get("Content-Length", -1)) != row.size_bytes:
        storage.delete(row.object_key)
        row.state = "failed"
        db.commit()
        raise HTTPException(409, "文件实际大小与声明不符")
    row.state = "completed"
    db.commit()
    return view(row)


@router.get("/agent/skills/uploads/{identifier}/preview")
def preview_personal(identifier: UUID, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor()
    return preview(db, identifier, access.user_id, access.workspace_id, "workspace")


@router.get("/site/admin/skills/uploads/{identifier}/preview")
def preview_site(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    return preview(db, identifier, actor.id, None, "site")


def preview(db, identifier, user_id, workspace_id, scope):
    row = owned(db, identifier, user_id, workspace_id, scope)
    if row.state != "completed":
        raise HTTPException(409, "请先完成上传")
    from .skill_files import text_content
    from .skill_discovery import parse
    from .site_skill_packages import review

    packages = load_packages(db, row)
    result = []
    for index, (folder, files) in enumerate(packages):
        manifest = text_content(files["SKILL.md"])
        if manifest is None:
            raise HTTPException(422, f"{folder} 中的 SKILL.md 需要是 UTF-8 文本")
        metadata = parse(manifest, "SKILL.md")
        result.append({"id": index, "folder": folder, "name": metadata["name"] or folder,
            "description": metadata["description"], "markdown": manifest,
            "resources": [{"path": path, "size": len(content)}
                for path, content in sorted(files.items()) if path != "SKILL.md"],
            "package_review": review(files)})
    return {"items": result}


@router.post("/agent/skills/uploads/{identifier}/import", status_code=201)
def import_personal(identifier: UUID, payload: ImportSelection, db=Depends(get_db),
                    access: Access = Depends(get_access)):
    access.editor()
    row = owned(db, identifier, access.user_id, access.workspace_id, "workspace")
    packages = load_packages(db, row)
    selected = select_packages(packages, payload.items)
    from .personal_skills import save_package
    results = [save_package(files, db, access) for _, files in selected]
    remove_upload(db, row)
    return {"items": results}


@router.post("/site/admin/skills/uploads/{identifier}/import", status_code=201)
def import_site(identifier: UUID, payload: ImportSelection, request: Request, db=Depends(get_db),
                actor=Depends(site_admin)):
    row = owned(db, identifier, actor.id, None, "site")
    packages = load_packages(db, row)
    selected = select_packages(packages, payload.items)
    from .site_skill_packages import encode_files, review
    from .models import SiteSkill, SiteSkillFile
    from .site_audit import record
    created = []
    for folder, files in selected:
        manifest = files["SKILL.md"].decode("utf-8")
        from .skill_discovery import parse
        metadata = parse(manifest, "SKILL.md")
        resources = {path: content for path, content in files.items() if path != "SKILL.md"}
        name = metadata["name"] or folder
        description = metadata["description"]
        if len(name) > 64 or len(description) > 1024:
            raise HTTPException(422, f"{folder} 的名称或简介过长，请先修改 SKILL.md")
        package_review = review(files)
        skill = SiteSkill(name=name, description=description, markdown=manifest,
            enabled=False, created_by=actor.id, updated_by=actor.id,
            files=[SiteSkillFile(path=path, content=content) for path, content in resources.items()])
        db.add(skill)
        db.flush()
        record(db, actor, "site.skill_created", "site_skill", skill.id, request=request,
            details={"fields": ["name", "description", "markdown", "files"], "version": skill.version,
                     "source": "package_import", "issues": package_review["issues"]})
        created.append({"id": str(skill.id), "name": skill.name})
    remove_upload(db, row, commit=False)
    db.commit()
    return {"items": created}


def load_packages(db, row):
    if row.state != "completed":
        raise HTTPException(409, "请先完成上传")
    with current_storage(db).download(row.object_key) as source:
        data = source.read(row.size_bytes + 1)
    if len(data) != row.size_bytes:
        raise HTTPException(409, "文件实际大小与声明不符")
    from .skill_files import unzip_packages
    if row.filename.lower().endswith(".md"):
        from .skill_files import text_content, validate_files
        if text_content(data) is None:
            raise HTTPException(422, "SKILL.md 需要是 UTF-8 文本")
        return [(row.filename, validate_files({"SKILL.md": data}))]
    return unzip_packages(data)


def select_packages(packages, indexes):
    if len(set(indexes)) != len(indexes) or any(index < 0 or index >= len(packages) for index in indexes):
        raise HTTPException(422, "所选 Skill 已变化，请重新预览")
    return [packages[index] for index in indexes]


def remove_upload(db, row, *, commit=True):
    storage = current_storage(db)
    storage.delete(row.object_key)
    row.state = "cleaned"
    if commit:
        db.commit()


@router.delete("/agent/skills/uploads/{identifier}", status_code=204)
def cancel_personal(identifier: UUID, db=Depends(get_db), access: Access = Depends(get_access)):
    access.editor()
    cancel(db, identifier, access.user_id, access.workspace_id, "workspace")


@router.delete("/site/admin/skills/uploads/{identifier}", status_code=204)
def cancel_site(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    cancel(db, identifier, actor.id, None, "site")


def cancel(db, identifier, user_id, workspace_id, scope):
    row = owned(db, identifier, user_id, workspace_id, scope)
    storage = current_storage(db)
    if row.state in ("uploading", "completing"):
        storage.abort(row.object_key, row.multipart_id)
    storage.delete(row.object_key)
    row.state = "cancelled"
    db.commit()


def cleanup_expired():
    with SessionLocal() as db:
        rows = db.scalars(select(SkillUpload).where(
            SkillUpload.expires_at <= now(), SkillUpload.state != "cleaned"
        ).with_for_update(skip_locked=True)).all()
        if not rows:
            return
        storage = current_storage(db)
        for row in rows:
            try:
                if row.state in ("uploading", "completing"):
                    storage.abort(row.object_key, row.multipart_id)
                storage.delete(row.object_key)
                row.state = "cleaned"
                db.commit()
            except Exception:
                db.rollback()
