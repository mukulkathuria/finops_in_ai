"""The LLM adapter contract (spec section 3): every adapter passes the same suite.

A new adapter is accepted only when it passes every test here against its recorded fixtures.
"""

import orjson
import pytest

from gateway.adapters.llm.gemini import decode_signature
from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    EmbedRequest,
    ImagePart,
    Message,
    ProviderError,
    ProviderErrorKind,
    TextPart,
    ToolCall,
    ToolSpec,
)
from tests.contract.providers import ERROR_SCENARIOS, Harness, build

WEATHER = ToolSpec(
    name="get_weather",
    description="Weather for a city",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


@pytest.fixture(params=["openai_compat", "anthropic", "gemini"])
def harness(request: pytest.FixtureRequest) -> Harness:
    return build(request.param)


def _req(model: str, *, tools: bool = False, **extra: object) -> CanonicalRequest:
    messages = (
        Message("system", (TextPart("You are terse."),)),
        Message("user", (TextPart("Say hello"),)),
    )
    return CanonicalRequest(
        model=model,
        messages=messages,
        max_tokens=64,
        tools=(WEATHER,) if tools else (),
        **extra,  # type: ignore[arg-type]
    )


async def _collect(harness: Harness, req: CanonicalRequest) -> list[CanonicalChunk]:
    return [chunk async for chunk in harness.adapter.stream(req)]


async def test_text_completion(harness: Harness) -> None:
    resp = await harness.adapter.complete(_req("text"))
    assert resp.content == "Hello there!"
    assert resp.finish_reason == "stop"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (12, 4)
    assert resp.tool_calls == ()


async def test_streamed_text_ends_with_finish_and_usage(harness: Harness) -> None:
    chunks = await _collect(harness, _req("text"))
    assert "".join(c.content or "" for c in chunks) == "Hello there!"
    finals = [c for c in chunks if c.finish_reason]
    assert [c.finish_reason for c in finals] == ["stop"]
    usage = next(c.usage for c in reversed(chunks) if c.usage)
    assert (usage.input_tokens, usage.output_tokens) == (12, 4)


async def test_tool_call(harness: Harness) -> None:
    resp = await harness.adapter.complete(_req("tools", tools=True))
    assert resp.finish_reason == "tool_calls"
    [call] = resp.tool_calls
    assert call.name == "get_weather"
    assert orjson.loads(call.arguments) == {"city": "Paris"}
    assert call.id


async def test_streamed_tool_call_reassembles(harness: Harness) -> None:
    chunks = await _collect(harness, _req("tools", tools=True))
    calls: dict[int, dict[str, str]] = {}
    for chunk in chunks:
        for delta in chunk.tool_calls:
            slot = calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
            slot["id"] = delta.id or slot["id"]
            slot["name"] = delta.name or slot["name"]
            slot["arguments"] += delta.arguments
    assert list(calls) == [0]
    assert calls[0]["name"] == "get_weather"
    assert orjson.loads(calls[0]["arguments"]) == {"city": "Paris"}
    assert [c.finish_reason for c in chunks if c.finish_reason] == ["tool_calls"]


async def test_usage_is_normalised_including_cache_and_reasoning(harness: Harness) -> None:
    resp = await harness.adapter.complete(_req("usage"))
    assert resp.usage == harness.expected_usage


@pytest.mark.parametrize(("scenario", "kind"), ERROR_SCENARIOS)
async def test_errors_are_typed(harness: Harness, scenario: str, kind: ProviderErrorKind) -> None:
    with pytest.raises(ProviderError) as err:
        await harness.adapter.complete(_req(scenario))
    assert err.value.kind is kind
    assert err.value.provider == harness.adapter.name


async def test_rate_limit_carries_retry_after(harness: Harness) -> None:
    with pytest.raises(ProviderError) as err:
        await harness.adapter.complete(_req("ratelimit"))
    assert err.value.retry_after_s == 7
    assert err.value.kind.retryable


async def test_streaming_errors_are_typed_too(harness: Harness) -> None:
    with pytest.raises(ProviderError) as err:
        await _collect(harness, _req("ratelimit"))
    assert err.value.kind is ProviderErrorKind.RATE_LIMIT


async def test_content_filter_is_reported(harness: Harness) -> None:
    if harness.content_filter_raises:
        with pytest.raises(ProviderError) as err:
            await harness.adapter.complete(_req("filter"))
        assert err.value.kind is ProviderErrorKind.CONTENT_FILTER
    else:
        assert (await harness.adapter.complete(_req("filter"))).finish_reason == "content_filter"


async def test_malformed_stream_is_a_typed_error(harness: Harness) -> None:
    with pytest.raises(ProviderError) as err:
        await _collect(harness, _req("malformed"))
    assert err.value.kind is ProviderErrorKind.OTHER


async def test_system_prompt_tools_and_images_are_mapped(harness: Harness) -> None:
    image = ImagePart("data:image/png;base64,iVBORw0KGgo=")
    req = CanonicalRequest(
        model="text",
        messages=(
            Message("system", (TextPart("You are terse."),)),
            Message("user", (TextPart("What is this?"), image)),
        ),
        max_tokens=32,
        tools=(WEATHER,),
        tool_choice="auto",
    )
    await harness.adapter.complete(req)
    body = harness.requests[-1]["body"]
    flat = orjson.dumps(body).decode()
    assert "You are terse." in flat
    assert "iVBORw0KGgo=" in flat
    assert "get_weather" in flat
    if harness.name == "openai_compat":
        assert body["messages"][0] == {"role": "system", "content": "You are terse."}
    elif harness.name == "anthropic":
        assert body["system"] == "You are terse."
        assert all(m["role"] != "system" for m in body["messages"])
    else:
        assert body["systemInstruction"] == {"parts": [{"text": "You are terse."}]}


async def test_tool_results_round_trip_in_history(harness: Harness) -> None:
    req = CanonicalRequest(
        model="text",
        messages=(
            Message("user", (TextPart("Weather?"),)),
            Message(
                "assistant", tool_calls=(ToolCall("call_9", "get_weather", '{"city": "Paris"}'),)
            ),
            Message("tool", (TextPart('{"temp_c": 21}'),), tool_call_id="call_9"),
        ),
        max_tokens=32,
        tools=(WEATHER,),
    )
    await harness.adapter.complete(req)
    flat = orjson.dumps(harness.requests[-1]["body"]).decode()
    assert "get_weather" in flat
    assert "temp_c" in flat


async def test_anthropic_never_sends_sampling_parameters() -> None:
    harness = build("anthropic")
    await harness.adapter.complete(_req("text", temperature=0.2, top_p=0.9))
    body = harness.requests[-1]["body"]
    assert "temperature" not in body
    assert "top_p" not in body
    assert harness.requests[-1]["headers"]["anthropic-version"] == "2026-10-21"
    assert not harness.adapter.capabilities("any").sampling_params


async def test_gemini_thought_signatures_survive_the_round_trip() -> None:
    harness = build("gemini")
    [call] = (await harness.adapter.complete(_req("tools", tools=True))).tool_calls
    assert decode_signature(call.id) == "c2lnbmF0dXJlLTE="
    history = CanonicalRequest(
        model="text",
        messages=(
            Message("user", (TextPart("Weather?"),)),
            Message("assistant", tool_calls=(call,)),
            Message("tool", (TextPart("sunny"),), tool_call_id=call.id),
        ),
        max_tokens=16,
    )
    await harness.adapter.complete(history)
    model_turn = harness.requests[-1]["body"]["contents"][1]
    assert model_turn["parts"][0]["thoughtSignature"] == "c2lnbmF0dXJlLTE="
    response_part = harness.requests[-1]["body"]["contents"][2]["parts"][0]["functionResponse"]
    assert response_part == {"name": "get_weather", "response": {"result": "sunny"}}


async def test_embeddings(harness: Harness) -> None:
    request = EmbedRequest(model="embed", inputs=("a", "b"), dimensions=2)
    if not harness.embeddings:
        with pytest.raises(ProviderError) as err:
            await harness.adapter.embed(request)
        assert err.value.kind is ProviderErrorKind.BAD_REQUEST
        return
    resp = await harness.adapter.embed(request)
    assert resp.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert resp.usage.input_tokens > 0
