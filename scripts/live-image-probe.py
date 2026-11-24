"""付费且显式的服务端图片传输探针，不修改平台设置。

在 API 容器内设置 IMAGE_PROBE_CONNECTION 和 IMAGE_PROBE_MODEL 后运行。
探针直接使用应用图片适配器，部署前可验证当前候选实现。
绝不输出凭据、提示词正文或图片内容。
"""
import json
import os
from uuid import UUID

from app.infra.db import SessionLocal
from app.images import adapters as adapter
from app.model_catalog.providers import _decrypt_api_key
from app.models import ModelConnection


def main():
    if os.environ.get("IMAGE_PROBE_PAID") != "yes":
        raise SystemExit("请设置 IMAGE_PROBE_PAID=yes，以确认允许执行计费的生成和编辑")
    with SessionLocal() as db:
        connection = db.get(ModelConnection, UUID(os.environ["IMAGE_PROBE_CONNECTION"]))
        if not connection or not connection.enabled:
            raise SystemExit("Configured connection unavailable")
        secret = _decrypt_api_key(connection.encrypted_api_key)
        snapshot = {"protocol": "openai_images", "model": os.environ["IMAGE_PROBE_MODEL"],
                    "base_url": connection.base_url, "timeout": 180}
    references = []
    for operation in ("generate", "edit"):
        payload = {"operation": operation, "prompt": "A simple red square on a white background." if operation == "generate"
                   else "Change the red square into a blue square. Keep the white background.",
                   "count": 1, "size": "1024x1024", "quality": "medium"}
        try:
            images, request_id, usage = adapter.generate(snapshot, secret, payload, references)
        except Exception as exc:
            print(json.dumps({"operation": operation, "ok": False, "error_type": type(exc).__name__,
                              "message": str(exc), "request_id": getattr(exc, "request_id", None)}), flush=True)
            return
        print(json.dumps({"operation": operation, "ok": bool(images), "images": len(images),
                          "bytes": [len(raw) for raw, _ in images], "request_id": request_id, "usage": usage}), flush=True)
        if not images:
            return
        references = [images[0]]


if __name__ == "__main__":
    main()
