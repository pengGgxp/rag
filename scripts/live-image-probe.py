"""Paid, explicit server-side transport probe; does not change platform settings.

Run inside an API container with IMAGE_PROBE_CONNECTION and IMAGE_PROBE_MODEL.
The adapter file is loaded alongside this script so a candidate can be verified
before deploying migrations. Never print credentials, prompt bodies, or images.
"""
import importlib.util
import json
import os
from pathlib import Path
from uuid import UUID

from app.db import SessionLocal
from app.llm_providers import _decrypt_api_key
from app.models import ModelConnection


def main():
    if os.environ.get("IMAGE_PROBE_PAID") != "yes":
        raise SystemExit("Set IMAGE_PROBE_PAID=yes to confirm billable generation and editing")
    spec = importlib.util.spec_from_file_location("candidate_image_adapters", Path(__file__).with_name("image_adapters.py"))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
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
