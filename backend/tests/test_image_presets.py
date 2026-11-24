import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.model_capabilities import ImageConfig
from app.images.presets import catalog
from app.images import adapters as image_adapters
from test_image_adapters import encoded_image
from test_shared_providers import setup_site
from test_auth import register


def config(identifier="gpt-image-2.5", **patch):
    p = next(p for p in catalog() if p["id"] == identifier)
    size, quality = p["sizes"][0]["value"], p["qualities"][0]["value"]
    return {"generate": True, "protocol": p["protocols"][0], "preset_id": identifier,
        "preset_revision": p["revision"], "default_size": size, "default_quality": quality,
        "specs": [{"operation": "generate", "size": size, "quality": quality, "credits_per_image": 0}], **patch}


@pytest.mark.parametrize("p", catalog(), ids=lambda p: p["id"])
def test_all_catalog_combinations(p):
    for size in p["sizes"]:
        for quality in p["qualities"]:
            parsed = ImageConfig.model_validate(config(p["id"], default_size=size["value"], default_quality=quality["value"],
                specs=[{"operation": "generate", "size": size["value"], "quality": quality["value"], "credits_per_image": 0}]))
            assert parsed.default_for("generate") == (size["value"], quality["value"])


@pytest.mark.parametrize("patch", [
    {"protocol": "gemini"}, {"preset_revision": 999}, {"preset_id": "unknown"},
    {"max_references": 4, "preset_id": "gemini-2.5-flash-image"},
    {"specs": [{"operation": "generate", "size": "invalid", "quality": "high", "credits_per_image": 1}]},
])
def test_invalid_presets(patch):
    with pytest.raises(ValidationError):
        ImageConfig.model_validate(config(**patch))


def test_independent_defaults_and_old_custom_config():
    legacy = {"generate": True, "edit": True, "specs": [
        {"operation": op, "size": "1024x1024", "quality": "standard", "credits_per_image": "3.25"} for op in ("generate", "edit")]}
    parsed = ImageConfig.model_validate(legacy)
    assert parsed.preset_id is None
    assert parsed.default_for("edit") == ("1024x1024", "standard")
    assert parsed.specs[0].credits_per_image == Decimal("3.25")
    newer = config(edit=True, defaults={"edit": {"size": "1536x1024", "quality": "high"}})
    newer["specs"].append({"operation": "edit", "size": "1536x1024", "quality": "high", "credits_per_image": 9})
    parsed = ImageConfig.model_validate(newer)
    assert parsed.default_for("generate") == ("1024x1024", "low")
    assert parsed.default_for("edit") == ("1536x1024", "high")
    newer["defaults"]["edit"]["size"] = "1024x1536"
    with pytest.raises(ValidationError):
        ImageConfig.model_validate(newer)


def test_duplicate_unpriced_and_revision_without_preset():
    row = config()
    row["specs"].append(row["specs"][0].copy())
    with pytest.raises(ValidationError):
        ImageConfig.model_validate(row)
    for patch in ({"specs": [{"operation": "generate", "size": "1024x1024", "quality": "low", "credits_per_image": None}]},
                  {"preset_id": None}):
        with pytest.raises(ValidationError):
            ImageConfig.model_validate(config(**patch))


def test_catalog_admin_only_and_returns_no_prices(accounts, monkeypatch):
    client, _, _ = setup_site(accounts, monkeypatch)
    path = "/site/admin/model-connections/image-presets"
    result = client.get(path)
    assert result.status_code == 200
    assert result.json() == catalog()
    assert "credits_per_image" not in result.text
    register(client, "reader@example.com")
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("identifier,quality,omit", [
    ("gemini-2.5-flash-image", "native", True), ("gemini-3-pro-image", "4K", False),
    ("gemini-3.1-flash-image", "512", False),
])
def test_gemini_wire_resolution(monkeypatch, identifier, quality, omit):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded_image()}}]}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(image_adapters.httpx, "stream", client.stream)
        image_adapters.generate({"model": identifier, "protocol": "gemini", "omit_resolution": omit,
            "base_url": "https://provider.example", "timeout": 10}, "hidden",
            {"prompt": "synthetic image", "count": 1, "size": "16:9", "quality": quality}, [])
    sent = requests[0]["generationConfig"]["imageConfig"]
    assert sent["aspectRatio"] == "16:9"
    assert ("imageSize" not in sent) if omit else sent["imageSize"] == quality


def test_save_conflict_task_defaults_and_price_snapshot(accounts, monkeypatch):
    from test_image_tasks import configure
    from app.models import SiteModelProvider, ImageTask, UserCreditAccount
    from sqlalchemy import select
    from app import llm_providers
    from app.images import tasks as image_tasks
    client, db, identity = configure(accounts, monkeypatch)
    row = db.scalar(select(SiteModelProvider))
    image = config("gemini-2.5-flash-image")
    image["specs"][0]["credits_per_image"] = 6
    image["defaults"] = {"generate": {"size": "1:1", "quality": "native"}}
    payload = {"name": row.name, "model": row.model, "connection_id": str(row.connection_id),
        "connection_version": 1, "version": row.version, "enabled": True, "supports_chat": False, "image_config": image}
    path = f"/site/admin/llm-providers/{row.id}"
    saved = client.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["image_config"]["preset_id"] == "gemini-2.5-flash-image"
    assert client.put(path, json=payload).status_code == 409
    balance = db.get(UserCreditAccount, UUID(identity["user"]["id"])).balance
    result = client.post("/image-tasks", json={"prompt": "synthetic", "idempotency_key": str(uuid4())})
    assert result.status_code == 202, result.text
    task = db.get(ImageTask, UUID(result.json()["task_id"]))
    assert task.payload["quality"] == "native" and task.payload["size"] == "1:1"
    assert task.snapshot["omit_resolution"] is True
    assert Decimal(task.snapshot["rate"]) == 6
    image["specs"][0]["credits_per_image"] = 15
    changed = client.put(path, json={**payload, "version": saved.json()["version"], "image_config": image})
    assert changed.status_code == 200, changed.text
    db.refresh(task)
    assert Decimal(task.snapshot["rate"]) == 6
    assert client.post("/image-tasks", json={"prompt": "invalid", "size": "1024x1024", "idempotency_key": str(uuid4())}).status_code == 422
    db.expire_all()
    assert db.get(UserCreditAccount, UUID(identity["user"]["id"])).balance == balance - 6
