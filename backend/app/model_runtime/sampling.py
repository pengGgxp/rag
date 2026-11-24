"""管理单轮模型采样的交互式传输与中断恢复。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import random
import time

import httpx
from httpx_sse import EventSource

from .client import LLMContextCapacityError, LLMProviderError
from app.infra.upstream_retries import context_capacity_failure, model_failure, retry_after

HTTP_RETRIES = 4
SAMPLING_RETRIES = 5
EVENT_IDLE_SECONDS = 300
_check_active = ContextVar("chat_sampling_check_active", default=None)


class SamplingError(LLMProviderError):
    def __init__(self, message, category="connection", after=None):
        super().__init__(message)
        self.category, self.after = category, after


class AuthenticationError(LLMProviderError):
    pass


def stream_closed():
    if _check_active.get() is None:
        return LLMProviderError("模型响应未完成，请继续提交")
    return SamplingError("模型连接已中断，可以继续提交")


def stream_error(error):
    if isinstance(error, (httpx.TransportError, TimeoutError)) and _check_active.get() is not None:
        return stream_closed()
    return LLMProviderError("模型流式连接或响应异常，请继续提交")


def incomplete_response(reason):
    if reason in {"content_filter", "content_policy_violation", "SAFETY", "RECITATION",
                  "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "refusal"}:
        return LLMProviderError("模型无法生成部分内容，请调整请求后继续")
    if reason in {"length", "max_output_tokens", "max_tokens", "MAX_TOKENS"}:
        return LLMProviderError("本次回答达到输出长度上限，可以要求继续或缩小任务范围")
    return LLMProviderError("模型响应未完成，请继续提交")


def upstream_error(error, *, status=None, after=None):
    if _check_active.get() is None or not isinstance(error, dict):
        return model_failure(status=status) if status else LLMProviderError("模型流式调用失败")
    codes = {str(error.get(key) or "").lower() for key in ("code", "type", "status")}
    if codes & {"context_length_exceeded", "context_window_exceeded", "prompt_too_long"}:
        return LLMContextCapacityError("请求超出模型上下文容量")
    if codes & {"usage_accounting_failed", "usage_not_available"}:
        return LLMProviderError("暂时无法确认本次模型用量，请联系管理员核对")
    if codes & {"content_filter", "content_policy_violation"}:
        return incomplete_response("content_filter")
    if status == 401 or codes & {"invalid_api_key", "authentication_error", "unauthenticated"}:
        return AuthenticationError("当前模型的凭据不可用，请切换模型或联系管理员")
    if status == 403 or codes & {"permission_error", "permission_denied"}:
        return LLMProviderError("当前模型的授权不可用，请切换模型或联系管理员")
    if codes & {"websocket_message_too_large", "websocket_upgrade_required", "websocket_connection_limit_reached"}:
        return SamplingError("模型连接需要切换传输方式", "http_fallback", after)
    if codes & {"insufficient_quota", "billing_hard_limit_reached", "usage_limit_reached"}:
        return LLMProviderError("当前模型额度已用尽，请切换模型或联系管理员")
    if status == 429 or codes & {"rate_limit_exceeded", "rate_limit_error", "resource_exhausted",
                                "overloaded_error", "engine_overloaded"}:
        return SamplingError("模型服务繁忙，可以稍后继续提交", "busy", after)
    if (status is not None and 500 <= status < 600) or codes & {"server_error", "internal_server_error"}:
        return SamplingError("模型服务暂不可用，可以稍后继续提交", "busy", after)
    return model_failure(status=status) if status else LLMProviderError("模型流式调用失败")


def delay_for(attempt, after):
    return after if after is not None else .2 * 2 ** (attempt - 1) * random.uniform(.9, 1.1)


def wait(seconds, check_active):
    end = time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        check_active()
        time.sleep(min(.2, remaining))
    check_active()


def sample(invoke, check_active, notify, *, provider=None, wait_for=None):
    wait_for = wait_for or wait
    token = _check_active.set(check_active)
    try:
        retries, attempt, network_delay = 0, 0, 5
        checked_credentials = False
        while True:
            check_active()
            try:
                return invoke(attempt)
            except AuthenticationError:
                refresh = getattr(provider, "refresh_credentials", None)
                if checked_credentials or refresh is None or not refresh():
                    raise
                checked_credentials = True
                attempt += 1
                socket = getattr(provider, "responses_socket", None)
                if socket:
                    socket.close()
                    socket.routing_token = None
            except SamplingError as error:
                attempt += 1
                socket = getattr(provider, "responses_socket", None)
                if socket and not socket.fallback and (error.category == "http_fallback" or retries == SAMPLING_RETRIES):
                    if error.after is not None:
                        wait_for(error.after, check_active)
                    socket.switch_to_http()
                    retries = 0
                    notify({"category": "http_fallback", "attempt": 0, "max_attempts": SAMPLING_RETRIES})
                    continue
                if error.category == "network_wait":
                    notify({"category": "network_wait", "attempt": 0, "max_attempts": SAMPLING_RETRIES})
                    wait_for(network_delay, check_active)
                    network_delay = min(network_delay * 2, 60)
                    continue
                if retries == SAMPLING_RETRIES:
                    raise
                retries += 1
                delay = delay_for(retries, error.after)
                if not socket or socket.fallback or retries > 1 or error.category != "connection":
                    notify({"category": error.category, "attempt": retries,
                            "max_attempts": SAMPLING_RETRIES, "wait_seconds": delay})
                wait_for(delay, check_active)
    finally:
        _check_active.reset(token)


def active_check():
    return _check_active.get()


def run_transport(runner, operation, check_active, *, idle=None):
    async def poll():
        task = asyncio.ensure_future(operation)
        try:
            async with asyncio.timeout(idle):
                while not task.done():
                    await asyncio.wait({task}, timeout=.25)
                    if not task.done():
                        check_active()
                return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return runner.run(poll())


class _Stream:
    """Drive an async transport from the existing synchronous agent worker."""
    def __init__(self, runner, response, check_active):
        self.runner, self.response, self.check_active = runner, response, check_active
        self.status_code, self.headers = response.status_code, response.headers

    def read(self):
        try:
            return run_transport(self.runner, self.response.aread(), self.check_active, idle=EVENT_IDLE_SECONDS)
        except TimeoutError as error:
            raise stream_closed() from error

    def iter_sse(self):
        events = EventSource(self.response).aiter_sse()
        try:
            while True:
                try:
                    yield run_transport(self.runner, anext(events), self.check_active, idle=EVENT_IDLE_SECONDS)
                except StopAsyncIteration:
                    return
                except TimeoutError as error:
                    raise stream_closed() from error
        finally:
            self.runner.run(events.aclose())


def stream_events(response):
    return response.iter_sse() if isinstance(response, _Stream) else EventSource(response).iter_sse()


@contextmanager
def model_stream(provider, method, url, **kwargs):
    check_active = _check_active.get()
    if check_active is None:
        from app.infra.upstream_retries import model_stream as shared_stream
        with shared_stream(provider, method, url, **kwargs) as response:
            yield response
        return
    with asyncio.Runner() as runner:
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15))
        try:
            for attempt in range(HTTP_RETRIES + 1):
                check_active()
                provider.check_available()
                provider.request_observation = {"attempts": attempt + 1, "accepted_response": False}
                manager = client.stream(method, url, **{key: value for key, value in kwargs.items() if key != "timeout"})
                try:
                    response = run_transport(runner, manager.__aenter__(), check_active)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
                    provider.request_observation["uncertain"] = not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
                    failure = (SamplingError("模型连接未能建立", "network_wait")
                               if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)) else stream_closed())
                    after = None
                else:
                    transport = _Stream(runner, response, check_active)
                    try:
                        if response.status_code < 400:
                            provider.request_observation["accepted_response"] = True
                            yield transport
                            return
                        if response.status_code in (400, 413, 429):
                            transport.read()
                        after = retry_after(response.headers.get("retry-after"))
                        try:
                            error_body = response.json()
                        except (ValueError, httpx.ResponseNotRead):
                            error_body = {}
                        error = error_body.get("error", {}) if isinstance(error_body, dict) else {}
                        failure = context_capacity_failure(response) or upstream_error(
                            error, status=response.status_code, after=after)
                        if not 500 <= response.status_code < 600:
                            raise failure from None
                    finally:
                        runner.run(manager.__aexit__(None, None, None))
                if attempt == HTTP_RETRIES:
                    raise failure from None
                wait(delay_for(attempt + 1, after), check_active)
        finally:
            runner.run(client.aclose())
