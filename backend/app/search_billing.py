"""Paid search calls join the model request's exact, idempotent settlement."""
from fractions import Fraction
import hashlib
import json

from sqlalchemy import select

from . import model_billing, web_search
from .credit_amounts import amount, exact_charge
from .credits import CreditError
from .models import ModelCallUsage, ModelUsageRequest, SearchToolCall, SiteSearchSettings
from .site_auth import protect, reveal
from .workspace_billing import multiplier


def accrued(db, request):
    total = Fraction()
    factor = multiplier(request.billing_context)
    for call in db.scalars(select(ModelCallUsage).where(ModelCallUsage.request_id == request.request_id)):
        for capability, field in zip(model_billing.CAPABILITIES, model_billing.USAGE_FIELDS):
            units = call.usage.get(field)
            if type(units) is int and units >= 0:
                total += exact_charge(units, call.prices[capability]) * factor
    for call in db.scalars(select(SearchToolCall).where(SearchToolCall.request_id == request.request_id)):
        if call.units or call.status == "started":
            total += exact_charge(call.units or 1, call.price) * factor
    return total


def begin(db, request_id, capability, key):
    request, account = model_billing._request_locked(db, request_id)
    previous = db.scalar(select(SearchToolCall).where(SearchToolCall.request_id == request_id,
        SearchToolCall.call_key == key))
    if previous:
        return previous, False
    if request.status != "running":
        raise web_search.SearchError("本次回答已经结束")
    settings = db.get(SiteSearchSettings, 1, populate_existing=True)
    snapshot = request.billing_context.get("search")
    if not settings or not settings.enabled or not snapshot or capability not in web_search.CAPABILITIES:
        raise web_search.SearchError("联网搜索暂不可用，请稍后重试")
    price = {**snapshot["prices"][capability], "version": snapshot["version"],
        "source": "platform_search", "source_id": "1"}
    reservations = sum((accrued(db, other) for other in db.scalars(select(ModelUsageRequest).where(
        ModelUsageRequest.workspace_id == request.workspace_id, ModelUsageRequest.status == "running"))), Fraction())
    reservations += sum((Fraction(other.credits) for other in db.scalars(select(ModelUsageRequest).where(
        ModelUsageRequest.workspace_id == request.workspace_id, ModelUsageRequest.status == "pending"))), Fraction())
    if amount(reservations + exact_charge(1, price) * multiplier(request.billing_context)) > account.balance + request.hold:
        raise CreditError()
    call = SearchToolCall(request_id=request_id, call_key=key, capability=capability, price=price)
    db.add(call)
    db.flush()
    return call, True


class SearchRuntime:
    def __init__(self, factory, access, request_id, config):
        self.factory, self.access, self.request_id, self.config = factory, access, request_id, config
        self.tools = web_search.tools() if config else []

    def execute(self, capability, arguments, call_key, check_active):
        check_active()
        key = hashlib.sha256(call_key.encode()).hexdigest()
        try:
            with self.factory() as db:
                self.access.revalidate(db)
                call, fresh = begin(db, self.request_id, capability, key)
                if not fresh:
                    if call.encrypted_result:
                        return json.loads(reveal(call.encrypted_result))
                    return {"error": "此前操作结果未确认，请勿重复执行"}
                identifier = call.id
                db.commit()
        except CreditError:
            return {"error": "积分不足，联网操作已停止，请充值后重试"}
        except web_search.SearchError as exc:
            return {"error": str(exc)}
        try:
            result = web_search.invoke(self.config, capability, arguments)
            status = "completed" if result["effective"] else "empty"
        except web_search.SearchError as exc:
            result, status = {"error": str(exc)}, "uncertain" if exc.uncertain else "failed"
        except Exception:
            result, status = {"error": "联网操作结果未确认，请稍后重试"}, "uncertain"
        # Save confirmed results before observing cancellation or sending them to the model.
        with self.factory() as db:
            request, _ = model_billing._request_locked(db, self.request_id)
            call = db.get(SearchToolCall, identifier)
            if request.status == "running" and call.status == "started":
                call.units = 1 if result.get("effective") else 0
                call.status = status
                call.encrypted_result = protect(json.dumps(result, ensure_ascii=False))
                db.commit()
        return result
