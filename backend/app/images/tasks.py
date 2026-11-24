"""管理可恢复的图片任务、供应商提交与精确结算。"""
import hashlib
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from ..identity.auth import Access
from app.billing.amounts import amount
from app.billing.credits import authorization_hold, ensure_account, release_hold, _entry
from app.infra.db import SessionLocal
from app.model_catalog.capabilities import ImageConfig
from ..models import ImageModelSettings, ImageTask, ModelConnection, SiteModelProvider, UsageEvent
from .schemas import ImageInput

TERMINAL = {"completed", "partial", "failed", "cancelled", "uncertain", "save_failed"}
logger = logging.getLogger("app.image_tasks")


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
    from ..tasks import process_image_task
    try:
        process_image_task.delay(str(identifier))
    except Exception:
        # 已持久化的排队任务可以安全地再次派发，Worker 只会领取一次。
        pass


def recover(factory=None, *, agent_only=False):
    """恢复文件交付或排队任务，不重放结果未确认的上游调用。"""
    from ..identity.auth import aware
    with (factory or SessionLocal)() as db:
        query = select(ImageTask).where(ImageTask.status.in_(("queued", "saving", "running")))
        if agent_only:
            query = query.where(ImageTask.agent_tool_execution_id.is_not(None))
        rows = db.scalars(query.with_for_update(skip_locked=True)).all()
        pending = []
        saving = []
        for row in rows:
            if row.agent_tool_execution_id:
                from ..agent.tool_jobs import owner_active
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
    from ..agent.tool_jobs import validate_owner
    validate_owner(db, agent_tool_execution_id, access)
    # 通过现有工作区账户串行化准入和幂等检查。
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
    from app.files.quotas import check_capacity
    from app.files.workspace import resolve_input
    check_capacity(db, access.user_id, 1)
    for reference in payload.references:
        resource, version = resolve_input(db, access, reference.model_dump(mode="json"))
        if version.content_type not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(422, "参考资源必须为 PNG、JPEG 或 WebP 图片")
    from app.billing.workspace import context_for_workspace
    context = context_for_workspace(db, access.user_id)
    rate = spec.credits_per_image * Decimal(context["multiplier_percent"]) / 100
    held = amount(rate * payload.count)
    identifier = uuid4()
    authorization_hold(db, access.user_id, access.user_id, identifier, held)
    snapshot = {"model": model.model, "model_version": model.version, "connection_id": str(connection.id),
        "connection_version": connection.version, "base_url": connection.base_url, "timeout": connection.timeout_seconds,
        "protocol": config.protocol, "image_model": config.image_model, "rate": str(rate), "cost_per_image": str(spec.cost_per_image) if spec.cost_per_image is not None else None,
        "billing_context": context, "spec": spec.model_dump(mode="json")}
    from .presets import find
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
    from app.operations.audit_log import record_audit
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
    from .adapters import generate, validate_image, ImageRejected, ImageUncertain
    from app.model_catalog.providers import _decrypt_api_key, _encrypt_api_key
    from app.files.workspace import resolve_input
    from app.files.cache import read_bytes
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
                from ..agent.tool_jobs import owner_active
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
            # read_bytes 会提交事务，因此提交上游请求前需要重新加锁领取任务。
            row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
            if row.status != "queued":
                return
            if row.cancel_requested:
                settle(db, row, 0)
                row.status = "cancelled"
                db.commit()
                return
            # 在进入可能计费的提交阶段前先解密本地凭据。
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
            # 结算和取消都先锁定任务，再锁定账户。
            row = db.get(ImageTask, identifier, with_for_update=True, populate_existing=True)
            row.upstream_request_id, row.usage = request_id or None, usage
            # 先持久化已校验图片，再尝试交付到对象存储。
            # 私有加密暂存会在资源提交时移除。
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
    from app.files.operations import workspace_lock
    factory = factory or SessionLocal
    with factory() as db:
        row = db.get(ImageTask, identifier)
        if row is None:
            return
        with workspace_lock(db, row.account_id):
            return _save_outputs(identifier, factory)


def _save_outputs(identifier, factory):
    from app.files.workspace import _stage_bytes, read_file
    from app.infra.storage import delete_object
    from app.model_catalog.providers import _decrypt_api_key
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
