"""Isolated HTTP origin. Only registered sandbox ports can be reached."""
import re
import secrets
from datetime import timedelta
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select

from .identity.auth import Access, aware, now
from app.cloud_computer.preview import Grant, Preview, digest, origin, owned
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from .identity.site_auth import reveal

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = "__Host-zk-preview"
MAX_BODY = 16 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
STRIP_REQUEST = HOP | {"host", "authorization", "cookie", "origin", "referer", "forwarded", "via", "content-length",
    "e2b-traffic-access-token", "x-access-token", "x-rag-request", "x-csrf-token"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "preview"}


def identifier(host):
    domain = re.escape(get_settings().preview_domain)
    match = re.fullmatch(r"([0-9a-f]{32})\." + domain, host.lower())
    if not match:
        raise HTTPException(404, "预览不存在")
    return UUID(match[1])


def check_grant(identifier, token, kind, exchange=False):
    from .models import CloudComputerSession, LoginSession
    with SessionLocal() as db:
        grant = db.get(Grant, digest(token), with_for_update=exchange)
        if not grant or grant.preview_id != identifier or grant.kind != kind or aware(grant.expires_at) <= now():
            raise HTTPException(401, "预览访问已过期，请从知库重新打开")
        preview = db.get(Preview, identifier)
        computer = db.get(CloudComputerSession, preview.session_id) if preview else None
        if not computer:
            raise HTTPException(404, "预览不存在")
        login = db.get(LoginSession, grant.session_hash)
        if not login or login.user_id != computer.user_id:
            raise HTTPException(403, "预览访问已失效，请重新打开预览")
        access = Access(computer.user_id, grant.session_hash).revalidate(db)
        row, computer = owned(db, access, identifier, live=True)
        if not row.upstream_host or not row.encrypted_traffic_token:
            raise HTTPException(503, "预览尚未就绪，请稍后刷新")
        # Defense in depth: the database cannot turn this gateway into a generic proxy.
        expected_suffix = f".{computer.snapshot['region']}.sandbox.aliyuncs.com"
        from app.cloud_computer.adapter import preview_ingress_port
        if (not row.upstream_host.endswith(expected_suffix)
                or not re.fullmatch(r"[a-zA-Z0-9.-]+", row.upstream_host)
                or not row.upstream_host.startswith(f"{preview_ingress_port(row.command_id)}-")):
            raise HTTPException(503, "预览暂不可用，请联系管理员")
        result = {"host": row.upstream_host, "token": reveal(row.encrypted_traffic_token),
            "origin": origin(row), "new_cookie": None}
        if exchange:
            cookie = secrets.token_urlsafe(32)
            db.delete(grant)
            db.add(Grant(digest=digest(cookie), preview_id=row.id, session_hash=access.session_hash,
                kind="cookie", expires_at=now() + timedelta(seconds=120)))
            db.commit()
            result["new_cookie"] = cookie
        return result


def request_headers(request, target):
    extra_hop = {item.strip().lower() for item in request.headers.get("connection", "").split(",")}
    headers = {key: value for key, value in request.headers.items()
        if key.lower() not in STRIP_REQUEST | extra_hop and not key.lower().startswith(("x-forwarded-", "sec-"))}
    # Application cookies stay on this isolated host; platform and gateway cookies never go upstream.
    cookies = SimpleCookie()
    try:
        cookies.load(request.headers.get("cookie", ""))
    except Exception:
        cookies = SimpleCookie()
    allowed = [f"{name}={value.coded_value}" for name, value in cookies.items() if name not in {COOKIE, "rag_session"}]
    if allowed:
        headers["cookie"] = "; ".join(allowed)
    if request.headers.get("origin") == target["origin"]:
        headers["origin"] = "https://" + target["host"]
    headers["e2b-traffic-access-token"] = target["token"]
    headers["x-zk-preview-host"] = urlsplit(target["origin"]).netloc
    return headers


def response_headers(response, target):
    extra_hop = {item.strip().lower() for item in response.headers.get("connection", "").split(",")}
    headers = {key: value for key, value in response.headers.items() if key.lower() not in HOP | extra_hop
        | {"set-cookie", "content-length", "content-security-policy", "content-security-policy-report-only",
            "x-frame-options", "access-control-allow-origin", "access-control-allow-credentials", "refresh",
            "report-to", "nel", "alt-svc", "service-worker-allowed"}}
    # Alibaba's port ingress forces HTML to download. This isolated origin serves pages.
    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type in {"text/html", "application/xhtml+xml"}:
        headers.pop("content-disposition", None)
    location = headers.get("location")
    if location:
        absolute = urlsplit(urljoin("https://" + target["host"] + str(response.request.url.raw_path.decode("ascii")), location))
        if absolute.scheme not in {"http", "https"} or absolute.netloc not in {target["host"], urlsplit(target["origin"]).netloc}:
            raise HTTPException(502, "网页跳转到了外部地址，已停止跳转")
        headers["location"] = target["origin"] + urlunsplit(("", "", absolute.path or "/", absolute.query, absolute.fragment))
    frontend = get_settings().frontend_url.rstrip("/")
    headers.update({"cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff",
        "content-security-policy": f"frame-ancestors {frontend}; object-src 'none'; base-uri 'self'; form-action 'self'; worker-src 'none'",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()"})
    return headers


def application_cookies(response):
    results = []
    for raw in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            continue
        for name, value in cookie.items():
            if name in {COOKIE, "rag_session"}:
                continue
            value["domain"] = ""
            value["secure"] = True
            value["samesite"] = "Strict"
            results.append(value.OutputString())
    return results


@app.exception_handler(HTTPException)
async def error_response(request, error):
    return PlainTextResponse(str(error.detail), status_code=error.status_code,
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"})


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    key = identifier(request.url.hostname or "")
    if request.headers.get("upgrade") or request.headers.get("sec-websocket-key"):
        raise HTTPException(426, "预览暂不支持实时连接，请使用手动刷新")
    if path == "_zk_preview/auth":
        if request.method != "GET":
            raise HTTPException(405, "请从知库重新打开预览")
        target = await anyio.to_thread.run_sync(check_grant, key, request.query_params.get("ticket", ""), "ticket", True)
        response = RedirectResponse("/", status_code=303, headers={"cache-control": "no-store", "referrer-policy": "no-referrer"})
        response.set_cookie(COOKIE, target["new_cookie"], httponly=True, secure=True, samesite="strict", path="/", max_age=3600)
        return response
    if path.startswith("_zk_preview/"):
        raise HTTPException(404, "预览入口不存在")
    if request.headers.get("sec-fetch-dest") == "serviceworker":
        raise HTTPException(403, "预览不支持后台服务")
    target = await anyio.to_thread.run_sync(check_grant, key, request.cookies.get(COOKIE, ""), "cookie")
    try:
        size = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "请求大小无效")
    if size < 0 or size > MAX_BODY:
        raise HTTPException(413, "请求内容过大，请缩小后重试")
    body = bytearray()
    with anyio.move_on_after(30) as scope:
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "请求内容过大，请缩小后重试")
    if scope.cancel_called:
        raise HTTPException(408, "上传超时，请重试")
    # raw_path preserves Chinese and escaped path segments; no browser input controls the host.
    raw_path = request.scope.get("raw_path", b"/").decode("ascii")
    query = request.scope.get("query_string", b"").decode("ascii")
    url = f"https://{target['host']}{raw_path}" + (f"?{query}" if query else "")
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False)
    upstream = None
    try:
        upstream = await client.send(client.build_request(request.method, url,
            headers=request_headers(request, target), content=bytes(body)), stream=True)
        if upstream.status_code == 101:
            raise HTTPException(426, "预览暂不支持实时连接，请使用手动刷新")
        if int(upstream.headers.get("content-length", "0")) > MAX_RESPONSE:
            raise HTTPException(502, "预览响应过大，请通过资源库下载文件")
        headers = response_headers(upstream, target)
    except BaseException as error:
        if upstream:
            await upstream.aclose()
        await client.aclose()
        if isinstance(error, httpx.HTTPError):
            raise HTTPException(502, "网页服务暂不可用，请回到对话检查服务后刷新") from None
        raise

    async def stream():
        received = 0
        try:
            with anyio.move_on_after(60):
                async for chunk in upstream.aiter_raw():
                    received += len(chunk)
                    if received > MAX_RESPONSE:
                        return
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response = StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
    for value in application_cookies(upstream):
        response.raw_headers.append((b"set-cookie", value.encode("latin-1")))
    return response
