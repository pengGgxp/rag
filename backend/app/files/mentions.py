"""Message resource references contain locating metadata, never file contents."""
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models import WorkspaceFile, WorkspaceFileVersion


def snapshot(row):
    return {"id": str(row.id), "filename": PurePosixPath(row.path).name, "path": row.path,
        "content_type": row.content_type, "size_bytes": row.size_bytes}


def catalog(db, access, query):
    from app.files.shared import refresh_index
    checked = access.revalidate(db)
    refresh_index(db, checked, query, 0, 20)
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.scalars(select(WorkspaceFile).join(WorkspaceFileVersion,
        WorkspaceFileVersion.file_id == WorkspaceFile.id).where(
        WorkspaceFile.owner_user_id == checked.user_id, WorkspaceFile.deleted_at.is_(None),
        WorkspaceFile.path.ilike(pattern, escape="\\"), WorkspaceFileVersion.version == 1,
        WorkspaceFileVersion.deleted_at.is_(None), WorkspaceFileVersion.available.is_not(False),
    ).order_by(WorkspaceFile.path, WorkspaceFile.id).limit(20)).all()
    return [snapshot(row) for row in rows]


def resolve(db, access, identifiers, *, skip_unavailable=False):
    from app.files.workspace import resolve_input
    checked = access.revalidate(db)
    mentions = []
    for identifier in dict.fromkeys(identifiers):
        try:
            row = db.get(WorkspaceFile, UUID(str(identifier)), populate_existing=True)
            if row is None or row.owner_user_id != checked.user_id or row.deleted_at:
                raise HTTPException(404, "资源不存在")
            row, _version = resolve_input(db, checked, {"id": str(row.id)})
        except HTTPException as error:
            if error.status_code not in (404, 409):
                raise
            if not skip_unavailable:
                raise HTTPException(409, "提及的资源文件已不可用，请重新选择") from None
            continue
        mentions.append(snapshot(row))
    return mentions
