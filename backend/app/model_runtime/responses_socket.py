"""管理单轮对话的 Responses WebSocket、响应游标和路由令牌。"""
import asyncio
import json
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from . import sampling as chat_sampling

ROUTE_HEADER = "x-codex-turn-state"


class ResponsesSocket:
    def __init__(self):
        self.runner = None
        self.connection = None
        self.last = None
        self.routing_token = None
        self.fallback = False

    def headers(self, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        if self.routing_token:
            headers[ROUTE_HEADER] = self.routing_token
        return headers

    def capture_route(self, headers):
        if not self.routing_token:
            self.routing_token = headers.get(ROUTE_HEADER)

    def close(self):
        if self.runner is not None:
            try:
                if self.connection is not None:
                    self.runner.run(self.connection.close())
            finally:
                self.runner.close()
        self.runner = self.connection = self.last = None

    def switch_to_http(self):
        self.close()
        self.fallback = True

    def record_response(self, request, response_id, output):
        self.last = ({key: value for key, value in request.items() if key not in ("input", "stream")},
                     [*request["input"], *output], response_id) if response_id else None

    def events(self, url, api_key, body):
        from ..upstream_retries import retry_after
        check = chat_sampling.active_check()
        if self.runner is None:
            self.runner = asyncio.Runner()

        def run(operation, idle=None):
            return chat_sampling.run_transport(self.runner, operation, check, idle=idle)

        try:
            if self.connection is None:
                parsed = urlsplit(url)
                socket_url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", *parsed[1:]))
                self.connection = run(connect(socket_url, additional_headers=self.headers(api_key), open_timeout=15,
                                              close_timeout=.5, max_size=16 * 1024 * 1024, proxy=None))
                self.capture_route(self.connection.response.headers)
            request = {key: value for key, value in body.items() if key != "stream"}
            if self.last:
                previous, prefix, response_id = self.last
                if (previous == {key: value for key, value in request.items() if key != "input"}
                        and request["input"][:len(prefix)] == prefix):
                    request = {**request, "input": request["input"][len(prefix):], "previous_response_id": response_id}
            await_response = self.connection.recv
            run(self.connection.send(json.dumps({"type": "response.create", **request})), chat_sampling.EVENT_IDLE_SECONDS)
            while True:
                body_event = json.loads(run(await_response(), chat_sampling.EVENT_IDLE_SECONDS))
                yield body_event
                if body_event.get("type") == "response.completed":
                    return
        except InvalidStatus as error:
            status = error.response.status_code
            after = retry_after(error.response.headers.get("Retry-After"))
            try:
                body_error = json.loads(error.response.body).get("error", {})
            except (ValueError, TypeError, AttributeError):
                body_error = {}
            if status in (404, 405, 426, 501):
                raise chat_sampling.SamplingError("模型连接需要切换传输方式", "http_fallback", after) from None
            raise chat_sampling.upstream_error(body_error, status=status, after=after) from None
        except ConnectionClosed as error:
            if error.rcvd and error.rcvd.code == 1009:
                raise chat_sampling.SamplingError("模型连接需要切换传输方式", "http_fallback") from None
            raise chat_sampling.stream_closed() from None
        except (OSError, TimeoutError, InvalidHandshake):
            raise chat_sampling.stream_closed() from None
