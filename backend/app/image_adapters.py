"""Bounded image transports. Never retry a potentially billable upstream call."""
import base64
import http.client
import io
import ipaddress
import json
import socket
import ssl
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

MAX_IMAGE = 20 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024


class ImageRejected(RuntimeError):
    pass


class ImageUncertain(RuntimeError):
    def __init__(self, message, request_id=None):
        super().__init__(message)
        self.request_id = request_id


def validate_image(raw):
    if len(raw) > MAX_IMAGE:
        raise ImageRejected("图片超过 20 MiB")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > 40_000_000:
                raise ImageRejected("图片格式或像素不受支持")
            format = image.format
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            clean = image.copy()
            clean.info.clear()
            output = io.BytesIO()
            clean.save(output, format=format)
        value = output.getvalue()
        if len(value) > MAX_IMAGE:
            raise ImageRejected("图片超过 20 MiB")
        return value, {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[format]
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageRejected("上游返回无效图片") from exc


def public_image(url):
    """Pin validated public DNS to the TLS connection; no redirects or proxy env."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ImageRejected("上游图片下载地址不受支持")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ImageRejected("上游图片下载地址不受支持")
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=60)
    raw_socket = socket.create_connection(addresses[0][4][:2], timeout=60)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
        connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), headers={"Accept": "image/*"})
        response = connection.getresponse()
        if response.status != 200:
            raise ImageRejected("上游图片暂时无法下载")
        raw = response.read(MAX_IMAGE + 1)
        if len(raw) > MAX_IMAGE:
            raise ImageRejected("上游图片过大")
        return raw
    finally:
        connection.close()
        raw_socket.close()


def generate(snapshot, secret, payload, references):
    protocol, model = snapshot["protocol"], snapshot["model"]
    root = snapshot["base_url"].rstrip("/")
    parameters = {"model": model, "prompt": payload["prompt"], "n": payload["count"],
                  "size": payload["size"], "quality": payload["quality"]}
    headers = {"Authorization": f"Bearer {secret}"}
    files, data = None, None
    if protocol == "openai_images":
        url = root + ("/images/edits" if references else "/images/generations")
        if references:
            files = [("image[]" if len(references) > 1 else "image", (f"reference-{i}.png", raw, mime)) for i, (raw, mime) in enumerate(references)]
            data, parameters = {k: str(v) for k, v in parameters.items()}, None
    elif protocol == "responses":
        url = root + "/responses"
        content = [{"type": "input_text", "text": payload["prompt"]}]
        content.extend({"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"} for raw, mime in references)
        parameters = {"model": model, "store": False, "input": [{"role": "user", "content": content}],
            "tools": [{"type": "image_generation", "model": snapshot["image_model"], "action": payload["operation"], "size": payload["size"], "quality": payload["quality"]}],
            "tool_choice": {"type": "image_generation"}}
    else:
        if not root.endswith(("/v1beta", "/v1")):
            root += "/v1beta"
        url = f"{root}/models/{quote(model.removeprefix('models/'), safe='')}:generateContent"
        headers = {"x-goog-api-key": secret}
        parts = [{"text": payload["prompt"]}] + [{"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode()}} for raw, mime in references]
        from .image_presets import resolution_options
        parameters = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"], "imageConfig": {
                "aspectRatio": payload["size"], **resolution_options(snapshot, payload["quality"])}}}
    request_id = ""
    try:
        with httpx.stream("POST", url, headers=headers, json=parameters, data=data, files=files,
                          timeout=snapshot["timeout"], follow_redirects=False) as response:
            request_id = response.headers.get("x-request-id", "")[:200]
            if response.status_code in (400, 401, 403, 404, 422, 429):
                raise ImageRejected(f"图片服务拒绝请求（HTTP {response.status_code}），请调整要求或联系管理员")
            if response.status_code != 200:
                raise ImageUncertain("图片服务结果尚未确认，请勿重复生成")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise ImageUncertain("图片响应过大，生成结果待确认")
        body = json.loads(raw)
        if protocol == "openai_images":
            entries = body.get("data", [])
        elif protocol == "responses":
            if body.get("status") != "completed":
                raise ImageUncertain("图片生成结果待确认")
            entries = [{"b64_json": item["result"]} for item in body.get("output", []) if item.get("type") == "image_generation_call" and item.get("result")]
            request_id = request_id or body.get("id", "")[:200]
        else:
            entries = [{"b64_json": part.get("inlineData", part.get("inline_data", {})).get("data")} for candidate in body.get("candidates", [])
                       for part in candidate.get("content", {}).get("parts", []) if "inlineData" in part or "inline_data" in part]
        if len(entries) > payload["count"]:
            raise ImageUncertain("图片数量与请求不一致，请核对结果")
        images = []
        rejected = 0
        for item in entries:
            if item.get("b64_json"):
                try:
                    image = base64.b64decode(item["b64_json"], validate=True)
                except ValueError:
                    rejected += 1
                    continue
            else:
                try:
                    image = public_image(item["url"])
                except ImageRejected as exc:
                    raise ImageUncertain("图片已返回但未能下载，请核对生成结果") from exc
            try:
                images.append(validate_image(image))
            except ImageRejected:
                rejected += 1
        usage = dict(body.get("usage", body.get("usageMetadata", {})) or {})
        if rejected:
            usage["rejected_outputs"] = rejected
        return images, request_id, usage
    except ImageUncertain as exc:
        exc.request_id = exc.request_id or request_id or None
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ImageUncertain("图片生成结果尚未确认，请勿重复生成", request_id or None) from exc
