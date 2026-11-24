import socket
import threading
import time
from types import SimpleNamespace

import anyio
import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP, Context
from sse_starlette.sse import AppStatus
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, RedirectResponse

from app.integrations import mcp_client


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com", "https://user:pass@example.com/mcp",
    "https://example.com/mcp?token=secret", "https://example.com/mcp#fragment",
    "https://example.com:0/mcp", "https://example.com\\@127.0.0.1/mcp",
])
def test_mcp_rejects_unsafe_connection_urls(url):
    with pytest.raises(mcp_client.MCPError):
        mcp_client.validate_url(url)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "192.168.1.10", "169.254.169.254", "100.100.100.200",
    "::1", "::ffff:127.0.0.1", "fe80::1", "0.0.0.0",
])
def test_private_dns_results_are_blocked(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443)),
    ])
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_allowed_private_hosts=""))
    with pytest.raises(mcp_client.MCPError):
        mcp_client.resolve_host("mcp.example.com", 443)


def test_mixed_dns_results_and_rebinding_are_blocked(monkeypatch):
    state = {"addresses": ["8.8.8.8"]}
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443)) for address in state["addresses"]
    ])
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_allowed_private_hosts=""))
    assert mcp_client.resolve_host("mcp.example.com", 443) == "8.8.8.8"
    state["addresses"] = ["8.8.8.8", "127.0.0.1"]
    with pytest.raises(mcp_client.MCPError):
        mcp_client.resolve_host("mcp.example.com", 443)
    state["addresses"] = ["127.0.0.1"]
    with pytest.raises(mcp_client.MCPError):
        mcp_client.resolve_host("mcp.example.com", 443)
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(mcp_allowed_private_hosts="mcp.example.com"))
    assert mcp_client.resolve_host("mcp.example.com", 443) == "127.0.0.1"


def test_transport_pins_validated_address_preserves_host_and_sni_and_rejects_other_origin(monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_client, "resolve_host", lambda *_: "8.8.8.8")

    async def check():
        transport = mcp_client.PinnedTransport("https://mcp.example.com/mcp")
        await transport.inner.aclose()
        transport.inner = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200))
        await transport.handle_async_request(httpx.Request("POST", "https://mcp.example.com/mcp"))
        assert seen[0].url.host == "8.8.8.8"
        assert seen[0].headers["host"] == "mcp.example.com"
        assert seen[0].extensions["sni_hostname"] == "mcp.example.com"
        with pytest.raises(mcp_client.MCPError):
            await transport.handle_async_request(httpx.Request("GET", "https://other.example.com/mcp"))
        await transport.aclose()

    anyio.run(check)


@pytest.mark.parametrize("schema", [
    {"$ref": "http://169.254.169.254/schema"},
    {"$defs": {"nested": {"$id": "https://attacker.example/schema", "$ref": "#/x"}}},
    {"items": {"$dynamicRef": "file:///etc/passwd"}},
])
@pytest.mark.parametrize("field", ["inputSchema", "outputSchema"])
def test_remote_schema_references_are_rejected(schema, field):
    tool = {"name": "unsafe", "inputSchema": {"type": "object"}, field: schema}
    with pytest.raises(mcp_client.MCPError, match="schema"):
        mcp_client.validate_tool(tool)


@pytest.fixture(params=["streamable_http", "sse"])
def live_mcp(request, monkeypatch):
    # SSE-Starlette 2.x caches a process-wide event; each test uses a new loop.
    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    secret = 'Bearer secret"quoted\\value'
    service = FastMCP("Protocol test", stateless_http=True, json_response=True)
    state = {"calls": 0}

    @service.tool(description=f"Add two numbers. {secret}")
    def calculate(a: int, b: int) -> int:
        """Add two numbers."""
        state["calls"] += 1
        return a + b

    @service.tool()
    def echo_auth(ctx: Context) -> str:
        return ctx.request_context.request.headers["authorization"]

    @service.tool()
    def fail() -> str:
        raise ValueError("Test remote tool failure")

    @service.tool()
    async def slow() -> str:
        await anyio.sleep(2)
        return "late"

    @service.tool()
    def large_result() -> str:
        return "长结果" * 70000 + "唯一尾部"

    transport = request.param
    app = service.sse_app() if transport == "sse" else service.streamable_http_app()

    async def redirect(_request):
        return RedirectResponse("http://external.invalid/mcp", status_code=307)

    app.add_route("/redirect", redirect, methods=["GET", "POST"])

    async def authenticate(req, next_handler):
        if req.headers.get("authorization") != secret:
            return Response(status_code=401)
        return await next_handler(req)

    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "MCP test server did not start"
        settings = mcp_client.get_settings().model_copy(update={
            "mcp_allowed_private_hosts": "127.0.0.1", "mcp_timeout_seconds": 5,
        })
        monkeypatch.setattr(mcp_client, "get_settings", lambda: settings)
        yield {
            "url": f"http://127.0.0.1:{port}/{'sse' if transport == 'sse' else 'mcp'}",
            "transport": transport, "headers": {"Authorization": secret}, "state": state,
            "settings": settings,
        }
    finally:
        server.should_exit = True
        thread.join(8)
        listener.close()
        assert not thread.is_alive(), "MCP test server leaked a thread"


def test_official_sdk_protocol_discovers_calls_and_redacts_credentials(live_mcp):
    connection = live_mcp
    args = (connection["url"], connection["transport"], connection["headers"])
    tools = mcp_client.discover(*args)
    calculate = next(item for item in tools if item["name"] == "calculate")
    assert calculate["description"] == "Add two numbers. [redacted]"
    result = mcp_client.call(*args, calculate, {"a": 3, "b": 4})
    assert result["isError"] is False
    assert result["structuredContent"] == {"result": 7}
    assert connection["state"]["calls"] == 1
    echo = next(item for item in tools if item["name"] == "echo_auth")
    result = mcp_client.call(*args, echo, {})
    assert connection["headers"]["Authorization"] not in str(result)
    assert result["structuredContent"]["result"] == "[redacted]"
    assert result["content"][0]["text"] == "[redacted]"
    failure = next(item for item in tools if item["name"] == "fail")
    assert mcp_client.call(*args, failure, {})["isError"] is True
    with pytest.raises(mcp_client.MCPError, match="变化"):
        mcp_client.call(*args, {**calculate, "description": "Unauthorized drift"}, {"a": 1, "b": 2})
    assert connection["state"]["calls"] == 1
    def revoked():
        raise mcp_client.MCPError("授权已撤销")
    with pytest.raises(mcp_client.MCPError, match="授权已撤销"):
        mcp_client.call(*args, calculate, {"a": 1, "b": 2}, revoked)
    assert connection["state"]["calls"] == 1


def test_official_sdk_authentication_and_timeout(live_mcp):
    connection = live_mcp
    with pytest.raises(mcp_client.MCPError):
        mcp_client.discover(connection["url"], connection["transport"], {})
    args = (connection["url"], connection["transport"], connection["headers"])
    slow = next(item for item in mcp_client.discover(*args) if item["name"] == "slow")
    connection["settings"].mcp_timeout_seconds = 0.5
    with pytest.raises(mcp_client.MCPError, match="未自动重试"):
        mcp_client.call(*args, slow, {})


def test_large_successful_mcp_result_is_not_a_serialized_character_failure(live_mcp):
    connection = live_mcp
    args = (connection["url"], connection["transport"], connection["headers"])
    large = next(item for item in mcp_client.discover(*args) if item["name"] == "large_result")
    result = mcp_client.call(*args, large, {})
    assert result["isError"] is False
    assert result["content"][0]["text"].endswith("唯一尾部")


def test_redirect_does_not_forward_credentials_to_another_origin(live_mcp, monkeypatch):
    hosts = []
    resolve = mcp_client.resolve_host

    def record(host, port):
        hosts.append(host)
        return resolve(host, port)

    monkeypatch.setattr(mcp_client, "resolve_host", record)
    live_mcp["settings"].mcp_timeout_seconds = 1
    with pytest.raises(mcp_client.MCPError):
        mcp_client.discover(
            live_mcp["url"].rsplit("/", 1)[0] + "/redirect",
            live_mcp["transport"], live_mcp["headers"],
        )
    assert hosts and set(hosts) == {"127.0.0.1"}
