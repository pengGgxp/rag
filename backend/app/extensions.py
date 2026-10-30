"""Retired personal extensions; historical database rows are deliberately retained."""
from fastapi import APIRouter, Depends, HTTPException

from .auth import get_access
from .site_skills import SkillRuntime as ExtensionRuntime

router = APIRouter(prefix="/agent", tags=["retired-extensions"])


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unavailable(path: str, access=Depends(get_access)):
    raise HTTPException(410, "个人扩展管理已关闭")


def validate_selection(db, access, skill_ids, server_ids):
    if skill_ids or server_ids:
        raise HTTPException(422, "无需选择个人扩展，请重新打开对话后重试")
