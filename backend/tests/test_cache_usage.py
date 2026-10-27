import asyncio

import httpx
import pytest

from app.llm import LLMProviderError, OpenAICompatibleChatProvider, _parse_usage, measured_usage
from test_llm import configured_settings


@pytest.mark.parametrize("cached", [0, 40, 100])
def test_cache_is_part_of_prompt_not_extra(cached):
    usage = _parse_usage({"prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": cached}, "prompt_cache_hit_tokens": cached})
    result = usage.measured()
    assert result["input_tokens"] == 100-cached
    assert result["cached_input_tokens"] == cached and result["cache_status"] == "reported"


@pytest.mark.parametrize("cached", [-1, True, 101, "40", {}, 1.5])
def test_bad_cache_falls_back_without_negative_or_discount(cached):
    usage = _parse_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": cached}})
    assert usage.measured()["input_tokens"] == 100
    assert usage.cached_input_tokens is None and usage.cache_status == "invalid"


def test_missing_conflicting_and_invalid_token_counts_are_distinct():
    assert _parse_usage({"prompt_tokens": 100}).cache_status == "missing"
    assert _parse_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 10}}).cache_status == "invalid"
    assert _parse_usage({"prompt_tokens": True}) is None


def test_estimate_includes_tools_but_never_guesses_cache_or_failed_usage():
    a = measured_usage(None, [], {}, [])
    b = measured_usage(None, [], {}, [{"description": "x"*500}])
    assert b["input_tokens"] > a["input_tokens"]
    assert b["cached_input_tokens"] is None and b["source"] == "estimate"
    failed = measured_usage(None, [], {}, success=False)
    assert failed["input_tokens"] is None and failed["completion_tokens"] is None
    assert failed["source"] == "unavailable"


def test_usage_survives_malformed_tool_arguments_and_resets_between_calls(monkeypatch):
    provider = OpenAICompatibleChatProvider(configured_settings())
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(200, json={
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        "choices": [{"message": {"tool_calls": [{"function": {"name": "x", "arguments": "bad"}}]}}]}))
    with pytest.raises(LLMProviderError):
        provider.complete_with_tools([], [])
    assert provider.last_usage.prompt_tokens == 100
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(500))
    with pytest.raises(LLMProviderError):
        provider.complete([])
    assert provider.last_usage is None


@pytest.mark.parametrize("tail", [None, {"prompt_tokens": True, "completion_tokens": 20}, {"total_tokens": 120}])
def test_stream_keeps_last_usage_only_even_with_empty_choices(monkeypatch, tail):
    import json
    payloads = [
        '{"choices":[{"delta":{"content":"ok"}}],"usage":{"prompt_tokens":50}}',
        '{"choices":[{"finish_reason":"stop","delta":{}}]}',
        '{"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":40}}}',
    ]
    if tail:
        payloads.append(json.dumps({"choices": [], "usage": tail}))
    transport = httpx.MockTransport(lambda req: httpx.Response(200,
        headers={"content-type":"text/event-stream"},
        text="".join(f"data: {item}\n\n" for item in payloads)+"data: [DONE]\n\n"))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    provider = OpenAICompatibleChatProvider(configured_settings())
    async def run():
        return [part async for part in provider.stream([])]
    assert asyncio.run(run()) == ["ok"]
    assert provider.last_usage.prompt_tokens == 100
    assert provider.last_usage.cached_input_tokens == 40
