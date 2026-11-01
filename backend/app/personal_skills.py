"""Private Skill drafts share package validation with platform methods."""
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select

from . import skill_files
from .auth import get_access
from .audit_log import record_audit
from .db import get_db
from .models import AgentSkill, AgentSkillFile, SiteSkill
from .site_skill_packages import decode_files, encode_files, review
from .site_skills import SkillWrite, SkillFileWrite, SkillPackageReview, Version, import_markdown

router = APIRouter(prefix="/agent/skills", tags=["personal-skills"])


class PersonalWrite(SkillWrite):
    limitations_acknowledged: bool = False


class PersonalUpdate(PersonalWrite):
    version: int = Field(ge=1, strict=True)


def owned(db, model, identifier, access, lock=False):
    query = select(model).where(model.id == identifier, model.owner_id == access.user_id,
        model.workspace_id == access.workspace_id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "扩展不存在或无权访问")
    return row


def audit(db, access, request, action, row):
    record_audit(db, workspace_id=access.workspace_id, actor_user_id=access.user_id,
        action=action, resource_type="agent_skill" if isinstance(row, AgentSkill) else "mcp_server",
        resource_id=row.id, metadata={"version": row.version}, request=request)


def package_review(files):
    result = review(files)
    resource_warning = "：暂不支持此资源，请移除并调整相关指令后再启用"
    issues = [issue.replace("请修改后再启用", "请检查该链接")
              for issue in result["issues"] if not issue.endswith(resource_warning)]
    for entry in result["manifest"]:
        if entry["readable"]:
            continue
        suffix = entry["path"].rsplit(".", 1)[-1].lower()
        if suffix in {"ps1", "cmd", "bat", "exe"}:
            notice = "暂不支持 Windows 脚本或程序，请改用 Python、Node.js 或 Shell。"
            issues.append(f"{entry['path']}：{notice}")
        elif entry["text"] and suffix in {"py", "js", "mjs", "cjs", "sh", "bash"}:
            notice = "脚本可按任务需要运行；所需依赖、文件和权限必须可用。"
        else:
            notice = "此文件随包保留，可查看或导出。"
        entry["personal_notice"] = notice
    return {**result, "can_enable": True,
        "has_limitations": bool(issues) or any(not entry["readable"] for entry in result["manifest"]),
        "issues": list(dict.fromkeys(issues))}


def read(row, detail=False):
    value = {"id": str(row.id), "name": row.name, "description": row.description,
        "enabled": row.enabled, "version": row.version, "updated_at": row.updated_at,
        "limitations_acknowledged": row.limitations_acknowledged}
    if detail:
        files = skill_files.bundle_files(row)
        value.update(markdown=row.markdown, files=encode_files(files), review=package_review(files))
    return value


@router.get("")
def listing(db=Depends(get_db), access=Depends(get_access)):
    return [read(row) for row in db.scalars(select(AgentSkill).where(
        AgentSkill.owner_id == access.user_id, AgentSkill.workspace_id == access.workspace_id)
        .order_by(AgentSkill.created_at.desc(), AgentSkill.id))]


@router.get("/platform")
def platform(db=Depends(get_db), access=Depends(get_access)):
    return [{"id": str(row.id), "name": row.name, "description": row.description, "version": row.version}
        for row in db.scalars(select(SiteSkill).where(SiteSkill.enabled.is_(True))
            .order_by(SiteSkill.created_at, SiteSkill.id))]


@router.get("/platform/{identifier}")
def platform_detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    from .site_skills import read as platform_read
    row = db.get(SiteSkill, identifier)
    if row is None or not row.enabled:
        raise HTTPException(404, "Skill 不存在或已停用")
    return platform_read(row, True)


@router.post("/import")
async def importing(file: UploadFile = File(...), access=Depends(get_access)):
    result = await import_markdown(file, access)
    result["review"] = package_review(decode_files(
        [SkillFileWrite(**item) for item in result["files"]], result["markdown"]))
    result["limitations_acknowledged"] = False
    return result


@router.post("/import-folder")
async def importing_folder(files: list[UploadFile] = File(...), paths: list[str] = Form(...),
                           access=Depends(get_access)):
    from io import BytesIO
    if len(files) != len(paths) or len(files) > skill_files.MAX_FILES:
        raise HTTPException(422, "请选择包含 SKILL.md 的单个文件夹")
    entries, total = [], 0
    for file, path in zip(files, paths):
        data = await file.read(skill_files.MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > skill_files.MAX_FILE_BYTES or total > skill_files.MAX_BUNDLE_BYTES:
            raise HTTPException(413, "Skill 文件超过大小限制")
        entries.append((path, data))
    contents, _ = skill_files.imported_files(entries)
    packed = skill_files.export_zip("skill", contents)
    return await importing(UploadFile(filename="skill.zip", file=BytesIO(packed)), access)


@router.post("/review")
def reviewing(payload: SkillPackageReview, access=Depends(get_access)):
    return package_review(decode_files(payload.files, payload.markdown))


def apply(row, payload):
    files = decode_files(payload.files, payload.markdown)
    checked = package_review(files)
    if payload.enabled and checked["has_limitations"] and not payload.limitations_acknowledged:
        raise HTTPException(422, "请先确认脚本会在当前工作区的临时隔离环境中运行，可能读取或生成工作区输出")
    previous = {item.path: item for item in row.files}
    for key in ("name", "description", "markdown", "enabled", "limitations_acknowledged"):
        setattr(row, key, getattr(payload, key))
    row.files = [previous.get(path) or AgentSkillFile(path=path) for path in files if path != "SKILL.md"]
    for item in row.files:
        item.content = files[item.path]


@router.post("", status_code=201)
def create(payload: PersonalWrite, request: Request, db=Depends(get_db), access=Depends(get_access)):
    from .storage_billing import check_capacity
    files = decode_files(payload.files, payload.markdown)
    check_capacity(db, access.workspace_id, sum(len(data) for data in files.values()))
    row = AgentSkill(owner_id=access.user_id, workspace_id=access.workspace_id, files=[], version=1)
    apply(row, payload)
    db.add(row)
    db.flush()
    audit(db, access, request, "agent_skill.created", row)
    db.commit()
    return read(row, True)


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    return read(owned(db, AgentSkill, identifier, access), True)


@router.put("/{identifier}")
def update(identifier: UUID, payload: PersonalUpdate, request: Request, db=Depends(get_db), access=Depends(get_access)):
    from .storage_billing import check_capacity, lock_workspace
    lock_workspace(db, access.workspace_id)
    row = owned(db, AgentSkill, identifier, access, True)
    if row.version != payload.version:
        raise HTTPException(409, "Skill 已更新，请重新加载后再保存")
    files = decode_files(payload.files, payload.markdown)
    check_capacity(db, access.workspace_id, sum(len(data) for data in files.values()),
        skill_replaced_bytes=len(row.markdown.encode("utf-8"))+sum(len(f.content) for f in row.files))
    apply(row, payload)
    row.version += 1
    audit(db, access, request, "agent_skill.updated", row)
    db.commit()
    return read(row, True)


@router.delete("/{identifier}", status_code=204)
def delete(identifier: UUID, payload: Version, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access, True)
    if row.version != payload.version:
        raise HTTPException(409, "Skill 已更新，请重新加载后再删除")
    audit(db, access, request, "agent_skill.deleted", row)
    db.delete(row)
    db.commit()


@router.api_route("/{identifier}/export", methods=["GET", "HEAD"])
def export(identifier: UUID, request: Request, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    from io import BytesIO
    from .storage_downloads import download_response
    data = skill_files.export_zip("skill", skill_files.bundle_files(row))
    return download_response(db, access, request, object_key="", size=len(data), filename=f"{row.name}.zip",
        content_type="application/zip", resource=f"skill:{row.id}:{row.version}", source_factory=lambda _: BytesIO(data))


@router.get("/{identifier}/files")
def file_read(identifier: UUID, path: str, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, AgentSkill, identifier, access)
    content = skill_files.bundle_files(row).get(skill_files.safe_path(path))
    if content is None:
        raise HTTPException(404, "文件不存在")
    return {"path": path, "text": skill_files.text_content(content)}
