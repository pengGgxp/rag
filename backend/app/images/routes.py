"""注册图片任务和站点图片模型管理接口。"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..auth import aware, get_access
from ..db import get_db
from ..model_capabilities import ImageConfig
from ..models import ImageModelSettings, ImageTask, ModelConnection, SiteModelProvider
from ..site_auth import site_admin
from .schemas import CreateInput, ImageInput, Resolution, SettingsInput
from .tasks import create, dispatch, owned, read, save_outputs, settle

router = APIRouter(prefix="/image-tasks", tags=["images"])
admin_router = APIRouter(prefix="/site/admin/image-models", tags=["site-admin"])


@router.post("", status_code=202)
def submit(payload: CreateInput, db=Depends(get_db), access=Depends(get_access)):
    return create(db, access, ImageInput.model_validate(payload.model_dump(exclude={"idempotency_key"})),
        str(payload.idempotency_key))


@router.get("/by-request/{key}")
def by_request(key: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = db.scalar(select(ImageTask).where(ImageTask.account_id == access.user_id,
        ImageTask.actor_user_id == access.user_id, ImageTask.idempotency_key == str(key)))
    if row is None:
        raise HTTPException(404, "图片请求尚未提交")
    if row.status in ("queued", "saving") and row.agent_tool_execution_id is None:
        dispatch(row.id)
    return read(row)


@router.get("/{identifier}")
def status(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier)
    if row.status == "running":
        from ..agent.tool_jobs import owner_active
        live = row.agent_tool_execution_id and owner_active(db, row.agent_tool_execution_id)
        if not live and aware(row.updated_at) < datetime.now(timezone.utc) - timedelta(seconds=row.snapshot["timeout"] + 300):
            row = owned(db, access, identifier, True)
            if row.status == "running" and aware(row.updated_at) < datetime.now(timezone.utc) - timedelta(seconds=row.snapshot["timeout"] + 300):
                row.status, row.error = "uncertain", "图片生成结果尚未确认，请联系管理员核对，勿重复生成"
            db.commit()
    if row.status in ("queued", "saving") and row.agent_tool_execution_id is None:
        dispatch(identifier)
    return read(row)


@router.post("/{identifier}/cancel")
def cancel(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier, True)
    row.cancel_requested = True
    if row.status == "queued":
        settle(db, row, 0)
        row.status = "cancelled"
    db.commit()
    return read(row)


@router.post("/{identifier}/retry-save")
def retry_save(identifier: UUID, db=Depends(get_db), access=Depends(get_access)):
    row = owned(db, access, identifier, True)
    if row.status != "save_failed":
        raise HTTPException(409, "此任务无需重试保存")
    row.status, row.error = "saving", None
    db.commit()
    if row.agent_tool_execution_id:
        # 保存重试只恢复文件交付，不会再次提交可能计费的生成请求。
        from sqlalchemy.orm import sessionmaker
        save_outputs(identifier, sessionmaker(bind=db.get_bind()))
        db.refresh(row)
    else:
        dispatch(identifier)
    return read(row)


@admin_router.get("")
def settings(db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(ImageModelSettings, 1)
    return {"version": row.version if row else 0,
        "generate_model_id": str(row.generate_model_id) if row and row.generate_model_id else None,
        "edit_model_id": str(row.edit_model_id) if row and row.edit_model_id else None}


@admin_router.put("")
def update_settings(payload: SettingsInput, db=Depends(get_db), actor=Depends(site_admin)):
    from ..model_pricing import settings_locked
    settings_locked(db)
    row = db.get(ImageModelSettings, 1, with_for_update=True)
    if payload.version != (row.version if row else 0):
        raise HTTPException(409, "图片默认模型已更新，请重新加载")
    if row is None:
        row = ImageModelSettings(id=1, version=0)
        db.add(row)
    for operation in ("generate", "edit"):
        identifier = getattr(payload, f"{operation}_model_id")
        if identifier:
            model = db.get(SiteModelProvider, identifier)
            config = ImageConfig.model_validate(model.image_config) if model else None
            connection = db.get(ModelConnection, model.connection_id) if model else None
            if (not model or not model.enabled or not config or not getattr(config, operation)
                    or not connection or not connection.enabled or not connection.encrypted_api_key):
                raise HTTPException(422, "请选择已启用并配置价格的图片模型")
        setattr(row, f"{operation}_model_id", identifier)
    row.version += 1
    from ..site_audit import record
    record(db, actor, "image_models.updated", "image_model_settings", 1,
        details={"fields": ["generate_model_id", "edit_model_id"]})
    db.commit()
    return settings(db, actor)


@admin_router.get("/tasks")
def admin_tasks(db=Depends(get_db), actor=Depends(site_admin)):
    rows = db.scalars(select(ImageTask).where(ImageTask.status.in_(("uncertain", "save_failed")))
        .order_by(ImageTask.created_at.desc()).limit(100))
    return [{**read(row), "account_id": str(row.account_id), "actor_user_id": str(row.actor_user_id),
        "upstream_request_id": row.upstream_request_id, "held_credits": str(row.held_credits),
        "requested_images": row.payload["count"]} for row in rows]


@admin_router.post("/tasks/{identifier}/resolve")
def resolve_task(identifier: UUID, payload: Resolution, db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
    if not row or row.status != "uncertain" or row.settled:
        raise HTTPException(409, "此任务已处理或无需核对")
    if payload.confirmed_images > row.payload["count"]:
        raise HTTPException(422, "确认图片数不能超过请求张数")
    settle(db, row, payload.confirmed_images)
    row.status = "save_failed" if row.outputs else "failed"
    row.error = "生成费用已核对；如仍需图片，请先核对已交付结果"
    from ..site_audit import record
    record(db, actor, "image_task.resolved", "image_task", identifier,
        details={"confirmed_images": payload.confirmed_images, "reason": payload.reason})
    db.commit()
    return read(row)
