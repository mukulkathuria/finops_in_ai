"""The streamed chat relay with the output guardrail (US-12): usage sent with every event does not
release a value held back, tool-call arguments are filtered too, a stream that ends without a
finish still delivers what was held, and the request settles exactly once.
Key-shaped strings are assembled at runtime so secret scanners never see them in the source."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    Message,
    TextPart,
    ToolCallDelta,
    Usage,
)
from gateway.pipeline.chat import _Relay, estimate_tokens
from gateway.pipeline.guardrails import OutputFilter

KEY = "AK" + "IA" + "QWERTYUIOPASDFGH"
REQUEST = CanonicalRequest(model="m", messages=(Message("user", (TextPart("hi"),)),), max_tokens=50)
USAGE = Usage(input_tokens=3, output_tokens=5)
EVERYTHING = frozenset({"pii", "secret"})


def _pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _run(chunks: list[CanonicalChunk]) -> tuple[list[CanonicalChunk], list[Any], _Relay]:
    """What the client receives, what the request settled with, and the relay."""
    ends: list[Any] = []
    entry = SimpleNamespace(cost=lambda usage: 1, model="m")
    call: Any = SimpleNamespace(r=SimpleNamespace(entry=entry), idem=None)

    async def upstream() -> AsyncIterator[CanonicalChunk]:
        for chunk in chunks[1:]:
            yield chunk

    output = OutputFilter(frozenset({"pii", "secret"}))
    relay = _Relay(call, REQUEST, chunks[0], upstream(), [], ends.append, output)
    return [chunk async for chunk in relay], ends, relay


def _never_leaks(released: list[str]) -> str:
    so_far = ""
    for piece in released:
        so_far += piece
        assert "AKIA" not in so_far, so_far  # not even the start of the key goes out
    return so_far


async def test_usage_with_every_event_does_not_release_a_held_value() -> None:
    text = f"Use {KEY} to deploy"
    chunks = [CanonicalChunk(content=p, usage=USAGE) for p in _pieces(text, 3)]
    chunks.append(CanonicalChunk(finish_reason="stop", usage=USAGE))
    out, ends, _ = await _run(chunks)
    assert _never_leaks([c.content or "" for c in out]) == "Use [REDACTED_SECRET] to deploy"
    assert out[-1].finish_reason == "stop"  # nothing follows the finish
    assert [e.status for e in ends] == ["ok"]


async def test_tool_call_arguments_are_filtered_and_complete_at_the_finish() -> None:
    arguments = json.dumps({"key": KEY, "note": "rotate it"})
    chunks = [CanonicalChunk(tool_calls=(ToolCallDelta(0, id="c1", name="save"),))]
    chunks += [
        CanonicalChunk(tool_calls=(ToolCallDelta(0, arguments=p),)) for p in _pieces(arguments, 4)
    ]
    chunks.append(CanonicalChunk(finish_reason="tool_calls", usage=USAGE))
    out, _, _ = await _run(chunks)
    released = [d.arguments for c in out for d in c.tool_calls]
    assert json.loads(_never_leaks(released)) == {"key": "[REDACTED_SECRET]", "note": "rotate it"}


async def test_a_stream_without_a_finish_still_delivers_what_was_held_and_settles_once() -> None:
    out, ends, relay = await _run([CanonicalChunk(content=p) for p in _pieces("mail a@x.io", 2)])
    assert "".join(c.content or "" for c in out) == "mail [EMAIL_1]"
    await relay.aclose()  # closing an ended stream changes nothing
    assert [(e.status, e.charged) for e in ends] == [("ok", True)]


async def test_text_held_back_when_a_stream_fails_still_counts_toward_the_estimate() -> None:
    ends: list[Any] = []
    entry = SimpleNamespace(cost=lambda usage: 1, model="m")
    call: Any = SimpleNamespace(r=SimpleNamespace(entry=entry), idem=None)

    async def upstream() -> AsyncIterator[CanonicalChunk]:
        raise ConnectionError("the provider dropped the stream")
        yield CanonicalChunk()  # pragma: no cover - makes this an async generator

    first = CanonicalChunk(content="Done. mail a@x.io")  # no usage: the gateway estimates
    output = OutputFilter(frozenset({"pii", "secret"}))
    relay = _Relay(call, REQUEST, first, upstream(), [], ends.append, output)
    with pytest.raises(ConnectionError):
        async for _ in relay:
            pass
    (end,) = ends
    assert end.status == "error"
    assert end.usage.output_tokens == estimate_tokens("Done. mail [EMAIL_1]")  # the held part too


async def test_a_stream_failing_while_all_its_output_is_held_is_still_charged() -> None:
    ends: list[Any] = []
    entry = SimpleNamespace(cost=lambda usage: 7, model="m")
    call: Any = SimpleNamespace(r=SimpleNamespace(entry=entry), idem=None)

    async def upstream() -> AsyncIterator[CanonicalChunk]:
        raise ConnectionError("the provider dropped the stream")
        yield CanonicalChunk()  # pragma: no cover - makes this an async generator

    first = CanonicalChunk(content="mail a@x.io")  # held whole: it touches the end of the stream
    relay = _Relay(call, REQUEST, first, upstream(), [], ends.append, OutputFilter(EVERYTHING))
    released: list[CanonicalChunk] = []

    async def read() -> None:
        async for chunk in relay:
            released.append(chunk)

    with pytest.raises(ConnectionError):
        await read()
    assert not any(c.content for c in released)  # nothing was released
    (end,) = ends
    assert (end.status, end.charged, end.cost) == ("error", True, 7)
