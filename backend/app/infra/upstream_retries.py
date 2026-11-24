"""One retry owner per safe upstream call; never replay an accepted model response."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import asyncio
import logging
import math
import random
import time

import httpx

logger = logging.getLogger("app.upstream_retries")
TRANSIENT_STATUS = frozenset((429, 500, 502, 503, 504))


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3  # Includes the initial request.
    base_delay: float = 1.0
    max_delay: float = 8.0
    seconds: float = 30.0
    retry_after_jitter: float = 0.0
    check_interval: float = 0.1


@dataclass(frozen=True)
class RetryHooks:
    check_active: object = None
    notify: object = None
    deadline: float | None = None  # Monotonic, if the owning task has a deadline.


_hooks = ContextVar("upstream_retry_hooks", default=RetryHooks())


@contextmanager
def retry_scope(check_active=None, notify=None, *, deadline=None):
    parent = _hooks.get()
    if parent.deadline is not None:
        deadline = min(parent.deadline, deadline) if deadline is not None else parent.deadline
    token = _hooks.set(RetryHooks(check_active or parent.check_active,
        notify if notify is not None else parent.notify,
        deadline if deadline is not None else parent.deadline))
    try:
        yield
    finally:
        _hooks.reset(token)


def retry_after(value, *, reject_negative=False):
    if not value:
        return None
    try:
        seconds = float(value)
        if reject_negative and seconds < 0:
            return None
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class RetryBudget:
    def __init__(self, kind, *, policy=None):
        self.kind, self.policy = kind, policy or RetryPolicy()
        self.hooks = _hooks.get()
        self.deadline = min(time.monotonic() + self.policy.seconds,
            self.hooks.deadline if self.hooks.deadline is not None else math.inf)
        self.attempt = 0

    def check(self):
        if self.hooks.check_active:
            self.hooks.check_active()

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def begin(self):
        self.check()
        if not self.remaining():
            return False
        self.attempt += 1
        if self.attempt > 1 and self.hooks.notify:
            self.hooks.notify({"kind": self.kind, "attempt": self.attempt,
                "max_attempts": self.policy.attempts, "wait_seconds": None})
        return True

    def delay(self, category, after=None, *, remaining_wait=math.inf):
        self.check()
        if self.attempt >= self.policy.attempts:
            return None
        # Equal jitter keeps a nonzero backoff while spreading simultaneous retries.
        cap = min(self.policy.max_delay, self.policy.base_delay * 2 ** (self.attempt - 1))
        delay = random.uniform(cap / 2, cap)
        if after is not None:
            # Never retry earlier than Retry-After; stop if it cannot fit the budget.
            if after > self.policy.max_delay:
                return None
            delay = max(delay, after)
            headroom = min(self.policy.retry_after_jitter, self.policy.max_delay - delay)
            if headroom > 0:
                delay += random.uniform(0, headroom)
        if delay >= self.remaining() or delay > remaining_wait:
            return None
        logger.info("Upstream retry kind=%s category=%s attempt=%s wait_seconds=%.3f",
            self.kind, category, self.attempt + 1, delay)
        if self.hooks.notify:
            self.hooks.notify({"kind": self.kind, "category": category,
                "attempt": self.attempt + 1, "max_attempts": self.policy.attempts,
                "wait_seconds": round(delay, 3)})
        return delay

    def wait(self, delay):
        end = time.monotonic() + delay
        while time.monotonic() < end:
            self.check()
            time.sleep(max(0, min(self.policy.check_interval, end - time.monotonic())))
        self.check()

    async def await_delay(self, delay):
        end = time.monotonic() + delay
        while time.monotonic() < end:
            self.check()
            await asyncio.sleep(max(0, min(.1, end - time.monotonic())))
        self.check()


@contextmanager
def retry_activity(check_active, emit, activity, *, finish=True):
    """Reuse the durable activity stream; add no activity for calls that never retry."""
    seen = False
    def notify(info):
        nonlocal seen
        seen = True
        delay = info["wait_seconds"]
        summary = (f"服务暂时繁忙，{math.ceil(delay)} 秒后重试（第 {info['attempt']}/{info['max_attempts']} 次）"
            if delay is not None else f"正在重试（第 {info['attempt']}/{info['max_attempts']} 次）")
        emit(("tool_started", {**activity, "status": "running", "summary": summary}))
    try:
        with retry_scope(check_active, notify):
            yield
    except BaseException as error:
        if seen and finish:
            from app.model_runtime.client import LLMProviderError
            from app.agent.loop import AgentCancelled
            cancelled = isinstance(error, AgentCancelled)
            emit(("tool_result", {**activity,
                "status": "cancelled" if cancelled else "error" if isinstance(error, LLMProviderError) else "interrupted",
                "summary": str(error) if isinstance(error, LLMProviderError) else "请求已停止" if cancelled else "请求已中断，请检查已有结果"}))
        raise
    else:
        if seen and finish:
            emit(("tool_result", {**activity, "status": "completed", "summary": "请求已恢复"}))


def model_failure(*, status=None, uncertain=False):
    from app.model_runtime.client import LLMProviderError
    if status == 400:
        message = "模型请求失败（HTTP 400），请调整输入或联系管理员检查模型调用配置"
    elif status in (401, 403):
        message = f"模型请求失败（HTTP {status}），请联系管理员检查模型访问权限"
    elif status == 429:
        message = "模型请求失败（HTTP 429），服务繁忙，请稍后重试"
    elif status is not None:
        message = f"模型请求失败（HTTP {status}），请稍后重试或联系管理员"
    elif uncertain:
        message = "模型响应传输中断，结果和用量尚未完全确认；请先检查已有回答再决定是否重试"
    else:
        message = "模型连接未能建立，请稍后重试"
    return LLMProviderError(message)


def context_capacity_failure(response):
    """Recognize explicit structured capacity errors, not arbitrary HTTP 400s."""
    if response.status_code not in (400, 413):
        return None
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            return None
        code = str(error.get("code") or "").lower()
        kind = str(error.get("type") or error.get("status") or "").lower()
        message = str(error.get("message") or "").lower()
        explicit = code in {"context_length_exceeded", "context_window_exceeded", "prompt_too_long", "input_too_long"}
        explicit |= kind == "invalid_request_error" and message.startswith("prompt is too long")
        explicit |= kind == "invalid_argument" and "input token count" in message and "exceeds" in message and "maximum" in message
        if explicit:
            from app.model_runtime.client import LLMContextCapacityError
            return LLMContextCapacityError("模型输入超出上下文容量，正在尝试整理当前上下文")
    except (ValueError, TypeError, AttributeError, httpx.ResponseNotRead):
        pass
    return None


def _model_decision(budget, response=None, error=None, provider=None):
    if response is not None:
        status = response.status_code
        if status < 400:
            return False, None, None
        failure = context_capacity_failure(response) or model_failure(status=status)
        category = getattr(failure, "code", None) or f"http_{status}"
        after = retry_after(response.headers.get("Retry-After"))
        safe = status in TRANSIENT_STATUS
    else:
        # Read/write/protocol failures can mean the provider already accepted a billable call.
        safe = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        category, after = "connection_not_sent" if safe else "response_uncertain", None
        failure = model_failure(uncertain=not safe)
    if provider is not None:
        provider.request_observation = {"attempts": budget.attempt, "category": category,
            "uncertain": not safe and response is None}
    delay = budget.delay(category, after) if safe else None
    return True, delay, failure


def _model_timeout(budget, configured):
    # Retry admission expires separately from a normal model response's read timeout.
    if budget.hooks.deadline is None:
        return configured
    remaining = budget.hooks.deadline - time.monotonic()
    if remaining <= 0:
        from app.model_runtime.client import LLMProviderError
        raise LLMProviderError("模型请求已超过任务截止时间")
    return min(configured, remaining)


def model_post(provider, url, **kwargs):
    budget = RetryBudget("model")
    failure = model_failure()
    while budget.begin():
        provider.check_available()
        provider.request_observation = {"attempts": budget.attempt, "accepted_response": False}
        response = None
        try:
            response = httpx.post(url, **{**kwargs, "timeout": _model_timeout(
                budget, kwargs.get("timeout", provider.timeout_seconds))})
        except httpx.TransportError as error:
            _, delay, failure = _model_decision(budget, error=error, provider=provider)
        else:
            if response.status_code < 400:
                provider.request_observation["accepted_response"] = True
                return response
            try:
                response.read()
                _, delay, failure = _model_decision(budget, response=response, provider=provider)
            finally:
                response.close()
        if delay is None:
            raise failure from None
        budget.wait(delay)
    raise failure from None


@contextmanager
def model_stream(provider, method, url, **kwargs):
    budget = RetryBudget("model")
    failure = model_failure()
    while budget.begin():
        provider.check_available()
        provider.request_observation = {"attempts": budget.attempt, "accepted_response": False}
        manager = httpx.stream(method, url, **{**kwargs, "timeout": _model_timeout(
            budget, kwargs.get("timeout", provider.timeout_seconds))})
        try:
            response = manager.__enter__()
        except httpx.TransportError as error:
            _, delay, failure = _model_decision(budget, error=error, provider=provider)
        else:
            if response.status_code < 400:
                provider.request_observation["accepted_response"] = True
                # The response is accepted. No retry surrounds reading, parsing or yielding.
                try:
                    yield response
                finally:
                    manager.__exit__(None, None, None)
                return
            try:
                response.read()
                _, delay, failure = _model_decision(budget, response=response, provider=provider)
            finally:
                manager.__exit__(None, None, None)
        if delay is None:
            raise failure from None
        budget.wait(delay)
    raise failure from None


@asynccontextmanager
async def model_astream(provider, client, method, url, **kwargs):
    budget = RetryBudget("model")
    failure = model_failure()
    while budget.begin():
        provider.check_available()
        provider.request_observation = {"attempts": budget.attempt, "accepted_response": False}
        manager = client.stream(method, url, **{**kwargs, "timeout": _model_timeout(
            budget, kwargs.get("timeout", provider.timeout_seconds))})
        try:
            response = await manager.__aenter__()
        except httpx.TransportError as error:
            _, delay, failure = _model_decision(budget, error=error, provider=provider)
        else:
            if response.status_code < 400:
                provider.request_observation["accepted_response"] = True
                try:
                    yield response
                finally:
                    await manager.__aexit__(None, None, None)
                return
            try:
                await response.aread()
                _, delay, failure = _model_decision(budget, response=response, provider=provider)
            finally:
                await manager.__aexit__(None, None, None)
        if delay is None:
            raise failure from None
        await budget.await_delay(delay)
    raise failure from None
