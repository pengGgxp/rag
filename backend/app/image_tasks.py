"""Durable, idempotent image work with one upstream submission and exact settlement."""
import hashlib
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .auth import Access, get_access
from .credit_amounts import amount
from .credits import authorization_hold, ensure_account, release_hold, _entry
from .db import get_db
from .model_capabilities import ImageConfig
from .models import ImageModelSettings, ImageTask, ModelConnection, SiteModelProvider, UsageEvent
from .site_auth import site_admin

router = APIRouter(prefix="/image-tasks", tags=["images"])
admin_router = APIRouter(prefix="/site/admin/image-models", tags=["site-admin"])
TERMINAL = {"completed", "partial", "failed", "cancelled", "uncertain", "save_failed"}
logger = logging.getLogger(__name__)


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    version: int = Field(ge=1, strict=True)


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(pattern="^(generate|edit)$", default="generate")
    prompt: str = Field(min_length=1, max_length=12000)
    size: str | None = Field(default=None, max_length=40)
    quality: str | None = Field(default=None, max_length=40)
    count: int = Field(default=1, ge=1, le=4, strict=True)
    references: list[Reference] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_operation(self):
        if not self.prompt.strip() or (self.operation == "edit") != bool(self.references):
            raise ValueError("编辑图片需要参考资源，生成图片不能携带参考资源")
        return self


class CreateInput(ImageInput):
    idempotency_key: UUID


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    generate_model_id: UUID | None = None
    edit_model_id: UUID | None = None


def selected_model(db, operation):
    settings = db.get(ImageModelSettings, 1)
    identifier = getattr(settings, f"{operation}_model_id", None)
    model = db.get(SiteModelProvider, identifier, populate_existing=True) if identifier else None
    connection = db.get(ModelConnection, model.connection_id, populate_existing=True) if model else None
    if not model or not model.enabled or not connection or not connection.enabled or not connection.encrypted_api_key:
        raise HTTPException(503, "图片服务尚未配置，请联系管理员")
    config = ImageConfig.model_validate(model.image_config)
    if not getattr(config, operation):
        raise HTTPException(503, "此图片能力尚未开放，请联系管理员")
    return model, connection, config


def read(row):
    labels = {"queued": "等待生成", "running": "正在生成图片", "saving": "正在保存图片", "completed": "图片已生成",
              "partial": "部分图片已生成", "failed": "图片未能生成", "cancelled": "已停止", "uncertain": "生成结果待确认", "save_failed": "图片已生成，保存未完成"}
    return {"task_id": str(row.id), "status": row.status, "summary": row.error or labels[row.status],
        "terminal": row.status in TERMINAL, "files": [o["resource"] for o in row.outputs if o.get("resource")],
        "credits": str(row.charged_credits), "cancel_requested": row.cancel_requested,
        "updated_at": row.updated_at.isoformat(), "view_url": "/resources"}


def tool_read(row):
    result = read(row)
    if row.status in ("failed", "cancelled", "uncertain", "save_failed"):
        result.update(error=result["summary"], error_code="image_" + row.status)
    if row.status not in TERMINAL or row.status == "uncertain":
        result.update(uncertain=True, error=result["summary"], error_code="image_result_uncertain")
    return result


def notify(row, callback):
    if callback:
        try:
            callback(read(row))
        except Exception:
            logger.warning("Image progress delivery deferred: %s", row.id)


def owned(db, access, identifier, lock=False):
    row = db.get(ImageTask, identifier, with_for_update=lock, populate_existing=True)
    if not row or row.actor_user_id != access.user_id:
        raise HTTPException(404, "图片任务不存在")
    return row


def dispatch(identifier):
    from .tasks import process_image_task
    try:
        process_image_task.delay(str(identifier))
    except Exception:
        # A durable queued task can be safely dispatched again; worker claims it once.
        pass


def recover(factory=None, *, agent_only=False):
    """Recover delivery or queued work, never replay an uncertain upstream call."""
    from .db import SessionLocal
    from .auth import aware
    with (factory or SessionLocal)() as db:
        query = select(ImageTask).where(ImageTask.status.in_(("queued", "saving", "running")))
        if agent_only:
            query = query.where(ImageTask.agent_tool_execution_id.is_not(None))
        rows = db.scalars(query.with_for_update(skip_locked=True)).all()
        pending = []
        saving = []
        for row in rows:
            if row.agent_tool_execution_id:
                from .agent_tool_jobs import owner_active
                active = owner_active(db, row.agent_tool_execution_id)
                if row.status == "queued" and not active:
                    settle(db, row, 0)
                    row.status, row.cancel_requested = "cancelled", True
                elif row.status == "saving" and not active:
                    saving.append(row.id)
                elif (row.status == "running" and not active and
                        aware(row.updated_at) < datetime.now(timezone.utc) - timedelta(seconds=row.snapshot["timeout"] + 300)):
                    row.status, row.error = "uncertain", "图片生成结果尚未确认，请联系管理员核对，勿重复生成"
                continue
            if row.status == "running":
                if aware(row.updated_at) < datetime.now(timezone.utc) - timedelta(seconds=row.snapshot["timeout"] + 300):
                    row.status, row.error = "uncertain", "图片生成结果尚未确认，请联系管理员核对，勿重复生成"
            else:
                pending.append(row.id)
        db.commit()
    for identifier in pending:
        dispatch(identifier)
    for identifier in saving:
        save_outputs(identifier, factory)


def create(db, access, payload, key, *, agent_tool_execution_id=None):
    access = access.revalidate(db)
    from .agent_tool_jobs import validate_owner
    validate_owner(db, agent_tool_execution_id, access)
    # Serialize admission and idempotency through the existing workspace account.
    ensure_account(db, access.user_id)
    digest = hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(ImageTask).where(ImageTask.account_id == access.user_id,
        ImageTask.actor_user_id == access.user_id, ImageTask.idempotency_key == key))
    if existing:
        if existing.content_hash != digest or existing.agent_tool_execution_id != agent_tool_execution_id:
            raise HTTPException(409, "此图片请求已提交，请勿更换内容重复提交")
        db.commit()
        if existing.status == "queued" and existing.agent_tool_execution_id is None:
            dispatch(existing.id)
        return read(existing)
    model, connection, config = selected_model(db, payload.operation)
    if payload.count > config.max_count or len(payload.references) > config.max_references:
        raise HTTPException(422, "图片张数或参考图数量超出当前模型限制")
    default_size, default_quality = config.default_for(payload.operation)
    size, quality = payload.size or default_size, payload.quality or default_quality
    spec = next((s for s in config.specs if (s.operation, s.size, s.quality) == (payload.operation, size, quality)), None)
    if spec is None:
        raise HTTPException(422, "当前图片规格不可用，请选择已配置的尺寸与质量")
    from .storage_billing import check_capacity
    from .workspace_files import resolve_input
    check_capacity(db, access.user_id, 1)
    for reference in payload.references:
        resource, version = resolve_input(db, access, reference.model_dump(mode="json"))
        if version.content_type not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(422, "参考资源必须为 PNG、JPEG 或 WebP 图片")
    from .workspace_billing import context_for_workspace
    context = context_for_workspace(db, access.user_id)
    rate = spec.credits_per_image * Decimal(context["multiplier_percent"]) / 100
    held = amount(rate * payload.count)
    identifier = uuid4()
    authorization_hold(db, access.user_id, access.user_id, identifier, held)
    snapshot = {"model": model.model, "model_version": model.version, "connection_id": str(connection.id),
        "connection_version": connection.version, "base_url": connection.base_url, "timeout": connection.timeout_seconds,
        "protocol": config.protocol, "image_model": config.image_model, "rate": str(rate), "cost_per_image": str(spec.cost_per_image) if spec.cost_per_image is not None else None,
        "billing_context": context, "spec": spec.model_dump(mode="json")}
    from .image_presets import find
    preset = find(config.preset_id)
    snapshot["omit_resolution"] = bool(preset and preset["omit_resolution"])
    snapshot["preset_id"] = config.preset_id
    snapshot["preset_revision"] = config.preset_revision
    row = ImageTask(id=identifier, account_id=access.user_id, actor_user_id=access.user_id,
        agent_tool_execution_id=agent_tool_execution_id,
        model_id=model.id, session_hash=access.session_hash, idempotency_key=key, content_hash=digest,
        status="queued", payload={**payload.model_dump(mode="json"), "size": size, "quality": quality},
        snapshot=snapshot, encrypted_credentials=connection.encrypted_api_key, held_credits=held,
        charged_credits=0, settled=False, cancel_requested=False, usage={}, outputs=[])
    db.add(row)
    from .audit_log import record_audit
    record_audit(db, owner_user_id=access.user_id, actor_user_id=access.user_id,
                 action="image.created", resource_type="image_task", resource_id=identifier)
    db.commit()
    if agent_tool_execution_id is None:
        dispatch(identifier)
    return read(row)


def settle(db, row, count):
    if row.settled:
        return
    account = ensure_account(db, row.account_id)
    credits = amount(Decimal(row.snapshot["rate"]) * count)
    release_hold(db, row.account_id, row.actor_user_id, row.id, row.held_credits)
    if credits:
        _entry(db, account, actor_user_id=row.actor_user_id, amount=-credits, kind="usage_settlement",
            idempotency_key=f"image:{row.id}:charge", metadata={"capability": "image_generation", "task_id": str(row.id), "images": count})
    db.add(UsageEvent(account_id=row.account_id, actor_user_id=row.actor_user_id, request_id=row.id,
        capability="image_generation" if row.payload["operation"] == "generate" else "image_edit", units=count,
        credits=credits, policy_version=row.snapshot["model_version"], policy_snapshot=row.snapshot,
        idempotency_key=f"image:{row.id}:usage", metadata_json={"task_id": str(row.id), "model_id": str(row.model_id)}))
    row.charged_credits, row.settled = credits, True


def task_access(db, row):
    return Access(row.actor_user_id, row.session_hash).revalidate(db)


def run(identifier, factory=None, *, agent_tool_execution_id=None, check_active=None, on_progress=None):
    from .db import SessionLocal
    from .image_adapters import generate, validate_image, ImageRejected, ImageUncertain
    from .llm_providers import _decrypt_api_key, _encrypt_api_key
    from .workspace_files import resolve_input
    from .resource_cache import read_bytes
    factory = factory or SessionLocal
    identifier = UUID(str(identifier))
    with factory() as db:
        row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
        if row and row.status == "saving":
            db.commit()
            save_outputs(identifier, factory)
            return
        if not row or row.status != "queued":
            return
        if row.agent_tool_execution_id != agent_tool_execution_id:
            return
        preparation_stage = "authorization"
        try:
            if row.agent_tool_execution_id:
                from .agent_tool_jobs import owner_active
                if not owner_active(db, row.agent_tool_execution_id):
                    row.cancel_requested = True
            if check_active:
                try:
                    check_active()
                except Exception:
                    row.cancel_requested = True
            access = task_access(db, row)
            model = db.get(SiteModelProvider, row.model_id, populate_existing=True)
            connection = db.get(ModelConnection, UUID(row.snapshot["connection_id"]), populate_existing=True)
            if not model or not model.enabled or not connection or not connection.enabled:
                raise HTTPException(409, "图片模型已停用")
            if row.cancel_requested:
                settle(db, row, 0)
                row.status = "cancelled"
                db.commit()
                return
            preparation_stage = "references"
            references = []
            for reference in row.payload["references"]:
                resource, version = resolve_input(db, access, reference)
                raw = read_bytes(db, access, resource, version)
                references.append(validate_image(raw))
            # read_bytes commits; claim again under a fresh lock before submission.
            row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
            if row.status != "queued":
                return
            if row.cancel_requested:
                settle(db, row, 0)
                row.status = "cancelled"
                db.commit()
                return
            # Resolve local credentials before crossing the billable submission boundary.
            preparation_stage = "credentials"
            secret = _decrypt_api_key(row.encrypted_credentials)
            preparation_stage = "authorization"
            access = task_access(db, row)
            if row.agent_tool_execution_id and not owner_active(db, row.agent_tool_execution_id):
                row.cancel_requested = True
            if check_active:
                try:
                    check_active()
                except Exception:
                    row.cancel_requested = True
            if row.cancel_requested:
                settle(db, row, 0)
                row.status = "cancelled"
                db.commit()
                notify(row, on_progress)
                return
            row.status = "running"
            db.commit()
            notify(row, on_progress)
        except Exception as exc:
            logger.warning("Image task preparation failed: task=%s stage=%s error_type=%s", identifier, preparation_stage, type(exc).__name__)
            db.rollback()
            row = db.get(ImageTask, identifier, with_for_update=True)
            if row.status == "queued":
                settle(db, row, 0)
                if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
                    message = exc.detail
                else:
                    message = {"authorization": "图片任务启动失败，请联系管理员并提供任务编号",
                        "references": "参考图片读取失败，请确认图片可用后重新选择",
                        "credentials": "图片服务凭据无法读取，请联系管理员检查服务加密配置"}[preparation_stage]
                row.status, row.error = "failed", message
                db.commit()
            return
        try:
            images, request_id, usage = generate(row.snapshot, secret, row.payload, references)
            # Settlement and cancellation take the task lock before the account lock.
            row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
            row.upstream_request_id, row.usage = request_id or None, usage
            # Keep verified bytes durable before attempting delivery to object storage.
            # This private encrypted staging is removed in the resource commit.
            row.outputs = [{"encrypted_data": _encrypt_api_key(base64.b64encode(raw).decode()),
                            "mime": mime, "size": len(raw)} for raw, mime in images]
            row.status = "saving"
            settle(db, row, len(images))
            db.commit()
            notify(row, on_progress)
        except ImageRejected as exc:
            db.rollback()
            row = db.get(ImageTask, identifier, with_for_update=True)
            settle(db, row, 0)
            row.status, row.error = "failed", str(exc)
            db.commit()
            return
        except Exception as exc:
            logger.warning("Image task result uncertain: task=%s error_type=%s", identifier, type(exc).__name__)
            db.rollback()
            row = db.get(ImageTask, identifier, with_for_update=True)
            if isinstance(exc, ImageUncertain) and exc.request_id:
                row.upstream_request_id = exc.request_id
            row.status, row.error = "uncertain", "图片生成结果尚未确认，请联系管理员核对，勿重复生成"
            db.commit()
            return
    save_outputs(identifier, factory)
    with factory() as db:
        notify(db.get(ImageTask, identifier), on_progress)


def save_outputs(identifier, factory=None):
    from .db import SessionLocal
    from .file_operations import workspace_lock
    factory = factory or SessionLocal
    with factory() as db:
        row = db.get(ImageTask, identifier)
        if row is None:
            return
        with workspace_lock(db, row.account_id):
            return _save_outputs(identifier, factory)


def _save_outputs(identifier, factory):
    from .db import SessionLocal
    from .workspace_files import _stage_bytes, read_file
    from .storage import delete_object
    from .llm_providers import _decrypt_api_key
    factory = factory or SessionLocal
    with factory() as db:
        row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
        if not row or row.status not in ("saving", "save_failed") or not row.settled:
            return
        uploaded = []
        try:
            access = task_access(db, row)
            outputs = []
            for index, output in enumerate(row.outputs):
                if output.get("resource"):
                    outputs.append(output)
                    continue
                data = base64.b64decode(_decrypt_api_key(output["encrypted_data"]), validate=True)
                suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[output["mime"]]
                resource = _stage_bytes(row.account_id, access, db, f"images/{row.id}/{index+1}.{suffix}", data, output["mime"], None, uploaded=uploaded)
                outputs.append({"mime": output["mime"], "size": output["size"], "resource": read_file(resource)})
            row.outputs = outputs
            row.status = "cancelled" if row.cancel_requested else "failed" if not outputs else "completed" if len(outputs) == row.payload["count"] else "partial"
            row.error = "未返回可保存的图片，未扣除生成积分" if not outputs else None
            db.commit()
        except Exception as exc:
            logger.warning("Image delivery failed: task=%s error_type=%s", identifier, type(exc).__name__)
            db.rollback()
            for key in uploaded:
                try:
                    delete_object(key)
                except Exception:
                    pass
            row = db.get(ImageTask, identifier, with_for_update=True)
            if row.status in ("saving", "save_failed"):
                row.status, row.error = "save_failed", "图片已生成但尚未保存，请恢复权限或空间后重试保存，无需再次生成"
            db.commit()
            return


@router.post("", status_code=202)
def submit(payload: CreateInput, db=Depends(get_db), access=Depends(get_access)):
    return create(db, access, ImageInput.model_validate(payload.model_dump(exclude={"idempotency_key"})), str(payload.idempotency_key))


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
        from .auth import aware
        from .agent_tool_jobs import owner_active
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
        # Delivery recovery never resubmits the billable generation.
        from sqlalchemy.orm import sessionmaker
        save_outputs(identifier, sessionmaker(bind=db.get_bind()))
        db.refresh(row)
    else:
        dispatch(identifier)
    return read(row)


@admin_router.get("")
def settings(db=Depends(get_db), actor=Depends(site_admin)):
    row = db.get(ImageModelSettings, 1)
    return {"version": row.version if row else 0, "generate_model_id": str(row.generate_model_id) if row and row.generate_model_id else None,
            "edit_model_id": str(row.edit_model_id) if row and row.edit_model_id else None}


@admin_router.put("")
def update_settings(payload: SettingsInput, db=Depends(get_db), actor=Depends(site_admin)):
    from .model_pricing import settings_locked
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
            if not model or not model.enabled or not config or not getattr(config, operation) or not connection or not connection.enabled or not connection.encrypted_api_key:
                raise HTTPException(422, "请选择已启用并配置价格的图片模型")
        setattr(row, f"{operation}_model_id", identifier)
    row.version += 1
    from .site_audit import record
    record(db, actor, "image_models.updated", "image_model_settings", 1, details={"fields": ["generate_model_id", "edit_model_id"]})
    db.commit()
    return settings(db, actor)


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_images: int = Field(ge=0, le=4, strict=True)
    reason: str = Field(min_length=5, max_length=300)


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
    from .site_audit import record
    record(db, actor, "image_task.resolved", "image_task", identifier,
           details={"confirmed_images": payload.confirmed_images, "reason": payload.reason})
    db.commit()
    return read(row)
