"""Resolve live base prices; freeze rates before billable work, never at settlement."""
from copy import deepcopy
from fractions import Fraction
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import BillingSnapshot, LLMProviderConfig, ModelBillingSettings, ModelConnection, SiteModelProvider, UsagePolicy, Workspace
from .llm import measured_usage
from .credit_amounts import allocate

MODEL_CAPABILITIES = ("llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens")
DOCUMENT_CAPABILITIES = ("ocr_pages", "embedding_tokens")


def shared_enabled(db):
    row = db.get(ModelBillingSettings, 1, populate_existing=True)
    return bool(row and row.shared_pricing_enabled)


def context_for_workspace(db, workspace_id):
    # The read lock lets a maintenance pause wait for admissions to commit.
    settings = db.scalar(select(ModelBillingSettings).where(ModelBillingSettings.id == 1)
                         .with_for_update(read=True).execution_options(populate_existing=True))
    if settings and settings.pricing_paused:
        raise HTTPException(503, "计费维护中，请稍后重试")
    workspace = db.get(Workspace, workspace_id, populate_existing=True)
    if workspace is None:
        raise HTTPException(404, "工作区不存在")
    shared = bool(settings and settings.shared_pricing_enabled)
    return {"source_mode": "shared" if shared else "legacy",
            "multiplier_percent": workspace.billing_multiplier_percent if shared else 100,
            "multiplier_version": workspace.billing_version,
            "workspace_id": str(workspace_id)}


def resolve_prices(db, workspace_id, capabilities, provider_id=None, *, context=None, required=True):
    from .model_connections import connection_for
    context = context or context_for_workspace(db, workspace_id)
    if (context.get("model_provider_id") == str(provider_id) and context.get("model_prices")
            and all(key in context["model_prices"] for key in capabilities)):
        if required and provider_id is not None:
            binding = db.get(LLMProviderConfig, provider_id)
            provider = db.get(SiteModelProvider, binding.site_provider_id, populate_existing=True) if binding else None
            connection_id = next((price.get("connection_id") for price in context["model_prices"].values()
                                  if price.get("connection_id")), None)
            connection = (db.get(ModelConnection, UUID(connection_id), populate_existing=True) if connection_id
                          else connection_for(db, provider, refresh=True) if provider else None)
            if (binding is None or binding.workspace_id != workspace_id or provider is None
                    or not provider.enabled or not connection or not connection.enabled):
                raise HTTPException(503, "当前模型已停用，请选择其他模型")
        return {key: deepcopy(context["model_prices"][key]) for key in capabilities}
    shared = context["source_mode"] == "shared"
    source, version, identifier, values = "legacy_workspace", 0, str(workspace_id), {}
    source_enabled = True
    if shared and provider_id is not None:
        binding = db.get(LLMProviderConfig, provider_id)
        if binding is None or binding.workspace_id != workspace_id or not binding.site_provider_id:
            raise HTTPException(503, "模型服务暂不可用，请联系站点管理员")
        provider = db.get(SiteModelProvider, binding.site_provider_id, populate_existing=True)
        if provider is None or (not provider.enabled and required):
            raise HTTPException(503, "当前模型已停用，请选择其他模型")
        connection = connection_for(db, provider, refresh=True)
        if required and (not connection or not connection.enabled):
            raise HTTPException(503, "模型服务已停用，请选择其他模型")
        source_enabled = provider.enabled and bool(connection and connection.enabled)
        source, version, identifier, values = "model_provider", provider.version, str(provider.id), provider.prices
    elif shared:
        if any(key in MODEL_CAPABILITIES for key in capabilities):
            raise HTTPException(503, "请在问答输入区选择可用模型")
        settings = db.get(ModelBillingSettings, 1, populate_existing=True)
        source, version, values = "platform_documents", settings.version, settings.template
        identifier = "1"
    else:
        rows = db.scalars(select(UsagePolicy).where(UsagePolicy.workspace_id == workspace_id,
            UsagePolicy.provider_id == provider_id, UsagePolicy.capability.in_(capabilities))
            .execution_options(populate_existing=True)).all()
        values = {p.capability: {"unit": p.unit, "unit_size": p.unit_size,
            "credits_per_unit": p.credits_per_unit, "enabled": p.enabled, "version": p.version} for p in rows}
    result = {}
    for key in capabilities:
        price = values.get(key)
        from .model_pricing import TemplatePrice
        try:
            normalized = TemplatePrice.model_validate({
                **{field: price[field] for field in ("unit", "unit_size", "credits_per_unit")},
                "capability": price.get("capability", key), "enabled": price.get("enabled", True)})
            valid = normalized.capability == key
        except (TypeError, ValueError, KeyError):
            valid = False
        if not valid or not price.get("enabled", True):
            if required:
                raise HTTPException(503, "服务暂不可用，请联系站点管理员完善计费配置")
        result[key] = {**(deepcopy(price) if valid else {"unit": "pages" if key == "ocr_pages" else "tokens",
            "unit_size": 1 if key == "ocr_pages" else 1000, "credits_per_unit": None, "enabled": True}),
            "capability": key, "configured": valid, "version": price.get("version", version) if valid else version,
            "source": source, "source_id": identifier,
            "provider_id": str(provider_id) if provider_id else None,
            "enabled": bool(price.get("enabled", True) if valid else True) and source_enabled}
        if shared and provider_id is not None:
            result[key].update(connection_id=str(provider.connection_id),
                               connection_version=connection.version if connection else 0,
                               api_protocol=connection.api_protocol if connection else None)
        if valid:
            result[key]["credits_per_unit"] = normalized.model_dump()["credits_per_unit"]
    return result


def operation_snapshot(db, workspace_id, request_id, event_key, *, capabilities=(), provider_id=None, context=None):
    # All callers serialize through the workspace account/request lock or ingestion job.
    existing = db.scalar(select(BillingSnapshot).where(BillingSnapshot.workspace_id == workspace_id,
        BillingSnapshot.request_id == request_id, BillingSnapshot.event_key == event_key))
    if existing:
        return existing
    context = deepcopy(context or context_for_workspace(db, workspace_id))
    prices = resolve_prices(db, workspace_id, capabilities, provider_id, context=context) if capabilities else {}
    if event_key == "model" and prices:
        context.update(model_provider_id=str(provider_id), model_prices=deepcopy(prices))
    row = BillingSnapshot(workspace_id=workspace_id, request_id=request_id, event_key=event_key,
                          context={**context, "prices": prices})
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.scalar(select(BillingSnapshot).where(BillingSnapshot.workspace_id == workspace_id,
            BillingSnapshot.request_id == request_id, BillingSnapshot.event_key == event_key))
        if row is None:
            raise
    return row


def document_snapshot(db, workspace_id, request_id, capability, event_key):
    return operation_snapshot(db, workspace_id, request_id, event_key, capabilities=(capability,)).context


def multiplier(context):
    return Fraction(context.get("multiplier_percent", 100), 100)


def finish_operation(db, workspace_id, request_id, key="model"):
    row = db.scalar(select(BillingSnapshot).where(BillingSnapshot.workspace_id == workspace_id,
        BillingSnapshot.request_id == request_id, BillingSnapshot.event_key == key))
    if row:
        row.status = "complete"


class LegacyPriceMeter:
    """Snapshot legacy calls without enabling cache discounts or v2 settlement."""
    def __init__(self, factory, workspace_id, request_id):
        self.factory, self.workspace_id, self.request_id = factory, workspace_id, request_id
        self.round = 0
        self.last_snapshot_id = None

    def invoke(self, provider, operation, messages, tools=None):
        from types import SimpleNamespace
        status = getattr(provider, "status", SimpleNamespace(provider_id=None, provider_key=None))
        provider_id = UUID(status.provider_id) if status.provider_id else None
        with self.factory() as db:
            root = db.scalar(select(BillingSnapshot).where(BillingSnapshot.workspace_id == self.workspace_id,
                BillingSnapshot.request_id == self.request_id, BillingSnapshot.event_key == "model"))
            if root is None:
                raise ValueError("Model request has no admission snapshot")
            if provider_id is None and root.context.get("model_provider_id"):
                provider_id = UUID(root.context["model_provider_id"])
                status = SimpleNamespace(provider_id=str(provider_id), provider_key=None)
            capabilities = MODEL_CAPABILITIES if root.context["source_mode"] == "shared" else (
                "llm_input_tokens", "llm_output_tokens")
            snapshot = operation_snapshot(db, self.workspace_id, self.request_id, f"model:{self.round}",
                capabilities=capabilities, provider_id=provider_id, context=root.context)
            self.last_snapshot_id = str(snapshot.id)
            snapshot_id = snapshot.id
            self.round += 1
            db.commit()
        provider.last_usage = None
        try:
            response = operation()
        except BaseException:
            usage = measured_usage(provider.last_usage, messages, {}, tools, success=False)
            if hasattr(provider, "thinking_diagnostic"):
                usage["thinking_diagnostic"] = provider.thinking_diagnostic()
            self._finish(snapshot_id, usage, status, "failed")
            raise
        output = {"content": response} if isinstance(response, str) else {
            "content": getattr(response, "content", None),
            "tool_calls": [call.arguments for call in getattr(response, "tool_calls", [])]}
        usage = measured_usage(getattr(response, "usage", None) or provider.last_usage, messages, output, tools)
        if hasattr(provider, "thinking_diagnostic"):
            usage["thinking_diagnostic"] = provider.thinking_diagnostic()
        if getattr(provider, "stream_diagnostic", None) == "upstream_returned_json":
            usage["stream_diagnostic"] = "upstream_returned_json"
        self._finish(snapshot_id, usage, status, "completed")
        return response

    def _finish(self, snapshot_id, usage, provider_status, status):
        with self.factory() as db:
            snapshot = db.get(BillingSnapshot, snapshot_id, with_for_update=True)
            snapshot.context = {**snapshot.context, "call_usage": {
                **usage, "provider_id": provider_status.provider_id,
                "provider_key": provider_status.provider_key, "billing_snapshot_id": str(snapshot_id)},
                "call_status": status}
            db.commit()


def legacy_call_usage(db, workspace_id, request_id):
    rows = [row for row in db.scalars(select(BillingSnapshot).where(
        BillingSnapshot.workspace_id == workspace_id, BillingSnapshot.request_id == request_id))
        if row.event_key.startswith("model:") and row.context.get("call_usage")]
    return [row.context["call_usage"] for row in sorted(rows, key=lambda row: int(row.event_key.split(":")[1]))]
