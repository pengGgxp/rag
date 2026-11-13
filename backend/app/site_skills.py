"""Administrative Skill uploads use the current workspace resource library too."""
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field

from .auth import get_access
from .db import get_db
from .site_auth import site_admin

router = APIRouter(prefix="/site/admin/skills", tags=["site-skills"], dependencies=[Depends(site_admin)])


class Version(BaseModel):
    # Used by the independent MCP management API.
    version: int = Field(ge=1, strict=True)


@router.get("")
def listing(db=Depends(get_db), access=Depends(get_access)):
    from .personal_skills import listing as resources
    return resources(db, access)


@router.post("/import", status_code=201)
def importing(file: UploadFile = File(...), db=Depends(get_db), access=Depends(get_access)):
    from .personal_skills import importing as upload
    return upload(file, db, access)


# Application methods still use load_skill. Uploaded files use ordinary file tools.
LOAD_SKILL = {"type": "function", "function": {"name": "load_skill",
    "description": "读取已连接应用提供的任务方法。资源库中的 SKILL.md 请用 list_files 和 read_file 读取。",
    "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}},
                   "required": ["skill_id"], "additionalProperties": False}}}
