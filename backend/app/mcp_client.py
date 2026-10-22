"""Official MCP transports with an origin-bound, DNS-pinned HTTP client."""

import hashlib
import ipaddress
import json
import re
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken
from jsonschema import Draft202012Validator, SchemaError
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .config import get_settings


class MCPError(RuntimeError):
    pass


def validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MCPError("MCP 地址格式不正确") from exc
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or (port is not None and port < 1)
        or any(char.isspace() for char in value) or "\\" in value
    ):
        raise MCPError("请使用不包含账号、查询参数或片段的 HTTP(S) 地址，认证信息通过请求头配置")
    return value


def _cipher() -> Fernet:
    secret = get_settings().mcp_credential_key
    if not secret:
        raise MCPError("管理员尚未配置 MCP 凭据加密密钥")
    try:
        return Fernet(secret.get_secret_value().encode())
    except (ValueError, TypeError) as exc:
        raise MCPError("MCP 凭据加密密钥格式不正确") from exc


def encrypt_headers(headers: dict[str, str]) -> str | None:
    for name, value in headers.items():
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
            or name.lower() in {
                "host", "cookie", "content-type", "content-length", "accept",
                "connection", "transfer-encoding", "proxy-authorization",
            }
            or name.lower().startswith(("mcp-", "proxy-", "x-forwarded-"))
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise MCPError("认证请求头名称或内容不合法")
    if not headers:
        return None
    return _cipher().encrypt(json.dumps(headers).encode()).decode()


def decrypt_headers(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        return json.loads(_cipher().decrypt(value.encode()))
    except (InvalidToken, ValueError) as exc:
        raise MCPError("MCP 凭据无法解密，请检查服务密钥或重新填写认证信息") from exc


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def resolve_host(host: str, port: int) -> str:
    trusted = {
        item.strip().lower().rstrip(".")
        for item in get_settings().mcp_allowed_private_hosts.split(",") if item.strip()
    }
    try:
        addresses = list(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ))
    except OSError as exc:
        raise MCPError("无法解析 MCP 服务地址") from exc
    if not addresses or (
        host.lower().rstrip(".") not in trusted
        and any(not _public_address(address) for address in addresses)
    ):
        raise MCPError("禁止连接内网、回环或云元数据地址")
    return addresses[0]


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str):
        self.origin = httpx.URL(endpoint)
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (url.scheme, url.host, url.port) != (
            self.origin.scheme, self.origin.host, self.origin.port,
        ):
            raise MCPError("MCP 请求不能离开已授权的服务地址")
        address = await anyio.to_thread.run_sync(resolve_host, url.host, url.port or (443 if url.scheme == "https" else 80))
        # Keep Host and TLS SNI while connecting to the address we just validated,
        # preventing a second DNS lookup from turning into an internal connection.
        pinned = httpx.Request(
            request.method, url.copy_with(host=address),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


@asynccontextmanager
async def session(endpoint: str, transport: str, headers: dict[str, str]):
    validate_url(endpoint)
    timeout = get_settings().mcp_timeout_seconds

    def client_factory(**kwargs):
        return httpx.AsyncClient(
            **{**kwargs, "headers": headers, "timeout": httpx.Timeout(timeout)},
            transport=PinnedTransport(endpoint), follow_redirects=False, trust_env=False,
        )

    with anyio.fail_after(timeout):
        if transport == "sse":
            async with sse_client(endpoint, httpx_client_factory=client_factory) as streams:
                async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=timeout)) as client:
                    await client.initialize()
                    yield client
        else:
            async with client_factory() as http:
                async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as client:
                        await client.initialize()
                        yield client


def tool_digest(tool: dict) -> str:
    return hashlib.sha256(json.dumps(tool, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_tool(tool: dict):
    for key in ("inputSchema", "outputSchema"):
        schema = tool.get(key)
        if schema is None and key == "outputSchema":
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise MCPError("MCP 工具参数或输出定义无效") from exc
        pending = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                # Even a fragment-only reference can escape if an enclosing $id
                # changes its base URI. MCP schemas must be self-contained.
                for keyword in ("$ref", "$dynamicRef", "$recursiveRef", "$id"):
                    value = item.get(keyword)
                    if isinstance(value, str) and value and not value.startswith("#"):
                        raise MCPError("MCP 工具定义不能引用外部 schema")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)


def redact_credentials(payload, headers):
    secrets = [value for value in headers.values() if value]
    secrets.extend(value[7:] for value in headers.values() if value.lower().startswith("bearer ") and value[7:])
    secrets = sorted(set(secrets + [
        json.dumps(value, ensure_ascii=False)[1:-1] for value in secrets
    ]), key=len, reverse=True)

    def redact(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        return value

    return redact(payload)


async def _tools(client: ClientSession, headers) -> list[dict]:
    result = []
    cursor = None
    seen = set()
    while True:
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            definition = redact_credentials(tool.model_dump(mode="json", exclude_none=True), headers)
            validate_tool(definition)
            result.append(definition)
        cursor = page.nextCursor
        if not cursor:
            return result
        if cursor in seen:
            raise MCPError("MCP 工具列表返回了重复分页游标")
        seen.add(cursor)


async def _discover(endpoint, transport, headers):
    async with session(endpoint, transport, headers) as client:
        return await _tools(client, headers)


def discover(endpoint: str, transport: str, headers: dict[str, str]) -> list[dict]:
    try:
        return anyio.run(_discover, endpoint, transport, headers)
    except MCPError:
        raise
    except Exception as exc:
        if error := _nested_error(exc):
            raise error from exc
        raise MCPError("MCP 连接或工具发现失败，请检查地址、认证和服务状态") from exc


def _nested_error(exc):
    if isinstance(exc, MCPError):
        return exc
    if isinstance(exc, ExceptionGroup):
        for child in exc.exceptions:
            if error := _nested_error(child):
                return error
    return None


async def _call(endpoint, transport, headers, tool, arguments, before_call):
    validate_tool(tool)
    async with session(endpoint, transport, headers) as client:
        current = next((item for item in await _tools(client, headers) if item["name"] == tool["name"]), None)
        if current is None or tool_digest(current) != tool_digest(tool):
            raise MCPError("工具定义已变化，请重新同步并授权")
        if before_call is not None:
            await anyio.to_thread.run_sync(before_call)
        result = await client.call_tool(tool["name"], arguments)
        payload = result.model_dump(mode="json", exclude_none=True)
        # A remote service may echo authentication values; do not propagate them.
        return redact_credentials(payload, headers)


def call(endpoint: str, transport: str, headers: dict[str, str], tool: dict, arguments: dict, before_call=None) -> dict:
    try:
        return anyio.run(_call, endpoint, transport, headers, tool, arguments, before_call)
    except MCPError:
        raise
    except Exception as exc:
        if error := _nested_error(exc):
            raise error from exc
        raise MCPError("MCP 工具调用失败或超时；未自动重试，请核对远端执行状态") from exc
