"""Administrator-reviewed text methods, loaded on demand without code execution."""
from datetime import datetime, timezone
from uuid import UUID

import frontmatter
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .db import get_db
from .models import SiteSkill
from .site_auth import site_admin
from .site_audit import record

router = APIRouter(prefix="/site/admin/skills", tags=["site-skills"])


class SkillWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    markdown: str = Field(min_length=1, max_length=50000)
    enabled: bool = False


class SkillUpdate(SkillWrite):
    version: int = Field(ge=1, strict=True)


class ImportedSkill(SkillWrite):
    description: str = Field(default="", max_length=1024)


class Version(BaseModel):
    version: int = Field(ge=1, strict=True)


def read(row, detail=False):
    result = {"id": str(row.id), "name": row.name, "description": row.description,
              "enabled": row.enabled, "version": row.version, "updated_at": row.updated_at}
    if detail:
        result["markdown"] = row.markdown
    return result


def find(db, identifier, lock=False):
    row = db.get(SiteSkill, identifier, with_for_update=lock, populate_existing=True)
    if row is None:
        raise HTTPException(404, "Skill 不存在")
    return row


@router.get("")
def listing(q: str = Query(default="", max_length=200), enabled: bool | None = None,
            page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
            db=Depends(get_db), actor=Depends(site_admin)):
    filters = []
    if q.strip():
        filters.append(SiteSkill.name.icontains(q.strip(), autoescape=True))
    if enabled is not None:
        filters.append(SiteSkill.enabled == enabled)
    total = db.scalar(select(func.count()).select_from(SiteSkill).where(*filters))
    rows = db.scalars(select(SiteSkill).where(*filters).order_by(SiteSkill.created_at.desc(), SiteSkill.id)
                      .offset((page - 1) * page_size).limit(page_size))
    return {"items": [read(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/import")
async def import_markdown(file: UploadFile = File(...), actor=Depends(site_admin)):
    if not (file.filename or "").lower().endswith(".md"):
        raise HTTPException(422, "请选择单个 Markdown 文件")
    data = await file.read(210001)
    if len(data) > 210000:
        raise HTTPException(422, "文件过大，请缩短指令后重试")
    try:
        post = frontmatter.loads(data.decode("utf-8-sig"))
        return ImportedSkill(name=post.get("name") or (file.filename or "Skill.md")[:-3],
                          description=post.get("description") or "",
                          markdown=post.content).model_dump()
    except Exception:
        raise HTTPException(422, "无法导入，请检查文件编码、名称、描述和正文长度") from None


@router.post("", status_code=201)
def create(payload: SkillWrite, request: Request, db=Depends(get_db), actor=Depends(site_admin)):
    row = SiteSkill(**payload.model_dump(), created_by=actor.id, updated_by=actor.id)
    db.add(row)
    db.flush()
    record(db, actor, "site.skill_created", "site_skill", row.id, request=request,
           details={"fields": list(type(payload).model_fields), "version": row.version})
    db.commit()
    return read(row, True)


@router.get("/{identifier}")
def detail(identifier: UUID, db=Depends(get_db), actor=Depends(site_admin)):
    return read(find(db, identifier), True)


@router.put("/{identifier}")
def update(identifier: UUID, payload: SkillUpdate, request: Request,
           db=Depends(get_db), actor=Depends(site_admin)):
    row = find(db, identifier, True)
    if row.version != payload.version:
        raise HTTPException(409, "Skill 已更新，请重新加载后再保存")
    fields = payload.model_dump(exclude={"version"})
    changed = [key for key, value in fields.items() if getattr(row, key) != value]
    for key, value in fields.items():
        setattr(row, key, value)
    row.version += 1
    row.updated_by, row.updated_at = actor.id, datetime.now(timezone.utc)
    record(db, actor, "site.skill_updated", "site_skill", row.id, request=request,
           details={"fields": changed, "version": row.version})
    db.commit()
    return read(row, True)


@router.delete("/{identifier}", status_code=204)
def delete(identifier: UUID, payload: Version, request: Request,
           db=Depends(get_db), actor=Depends(site_admin)):
    row = find(db, identifier, True)
    if row.version != payload.version:
        raise HTTPException(409, "Skill 已更新，请重新加载后再删除")
    record(db, actor, "site.skill_deleted", "site_skill", row.id, request=request,
           details={"version": row.version})
    db.delete(row)
    db.commit()


LOAD_SKILL = {"type": "function", "function": {"name": "load_skill",
    "description": "Read the full platform-approved text method when its description matches the user's task. Does not execute commands or grant permissions.",
    "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}},
                   "required": ["skill_id"], "additionalProperties": False}}}


class SkillRuntime:
    def __init__(self, factory, access, *_unused):
        self.factory, self.access = factory, access
        with factory() as db:
            access.revalidate(db)
            self.snapshots = {str(row.id): {**read(row), "instructions": row.markdown}
                for row in db.scalars(select(SiteSkill).where(SiteSkill.enabled.is_(True))
                    .order_by(SiteSkill.created_at, SiteSkill.id))}
        self.skills = [{key: value for key, value in row.items() if key in ("id", "name", "description", "version")}
                       for row in self.snapshots.values()]
        self.tools = [LOAD_SKILL] if self.skills else []

    def label(self, name, arguments=None):
        row = self.snapshots.get(str((arguments or {}).get("skill_id")))
        return f"使用「{row['name']}」" if row else "读取任务方法"

    def execute(self, name, arguments, check_active=None):
        if check_active:
            check_active()
        if name != "load_skill" or not isinstance(arguments, dict) or set(arguments) != {"skill_id"}:
            return {"error": "该能力不可用"}
        snapshot = self.snapshots.get(str(arguments["skill_id"]))
        if snapshot is None:
            return {"error": "该方法不在本次可用范围内"}
        with self.factory() as db:
            self.access.revalidate(db)
            current = db.get(SiteSkill, UUID(snapshot["id"]), populate_existing=True)
            if current is None or not current.enabled:
                return {"error": "该方法已停用，请继续使用其他方式处理"}
        return {key: snapshot[key] for key in ("id", "name", "version", "instructions")}
