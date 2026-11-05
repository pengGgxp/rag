import base64
import io

import httpx
import pytest
from PIL import Image

from app import image_adapters as adapter


def encoded_image():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode()


@pytest.mark.parametrize("protocol", ["openai_images", "gemini", "responses"])
def test_three_wire_formats(monkeypatch, protocol):
    requests = []
    encoded = encoded_image()
    body = {"openai_images": {"data": [{"b64_json": encoded}]},
            "gemini": {"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded, "mimeType": "image/png"}}]}}]},
            "responses": {"id": "response-id", "status": "completed", "output": [{"type": "image_generation_call", "result": encoded}]}}[protocol]
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=body, headers={"x-request-id": "request-id"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(adapter.httpx, "stream", client.stream)
        images, request_id, usage = adapter.generate({"protocol": protocol, "model": "configured-model", "image_model": "configured-image-tool",
            "base_url": "https://provider.example/v1", "timeout": 60}, "hidden-key",
            {"prompt": "a blue square", "count": 1, "size": "1024x1024", "quality": "standard", "operation": "generate"}, [])
    assert len(images) == 1 and images[0][1] == "image/png"
    assert request_id == "request-id" and len(requests) == 1
    import json
    sent = json.loads(requests[0].content)
    if protocol == "responses":
        assert sent["tools"][0]["model"] == "configured-image-tool"
        assert sent["tools"][0]["action"] == "generate"
    elif protocol == "gemini":
        assert requests[0].headers["x-goog-api-key"] == "hidden-key"
    else:
        assert sent["n"] == 1


def test_invalid_output_does_not_discard_valid_partial_result(monkeypatch):
    def respond(request):
        return httpx.Response(200, json={"data": [{"b64_json": encoded_image()}, {"b64_json": base64.b64encode(b"invalid").decode()}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(adapter.httpx, "stream", client.stream)
        images, _, usage = adapter.generate({"protocol": "openai_images", "model": "m", "base_url": "https://provider.example/v1", "timeout": 60}, "key",
            {"prompt": "p", "count": 2, "size": "1024x1024", "quality": "standard"}, [])
    assert len(images) == 1 and usage["rejected_outputs"] == 1


@pytest.mark.parametrize("url", ["http://example.com/image", "https://127.0.0.1/image", "https://user:pass@example.com/image", "https://example.com:444/image"])
def test_private_or_unsafe_result_urls_are_rejected(url):
    with pytest.raises(adapter.ImageRejected):
        adapter.public_image(url)
