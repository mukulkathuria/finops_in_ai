"""Idempotency keys: header validation, request fingerprints and exact result replays."""

from datetime import UTC, datetime

import pytest

from gateway.domain.errors import InvalidInput
from gateway.domain.idempotency import Idempotency
from gateway.domain.llm import CanonicalResponse, EmbedResponse, ToolCall, Usage
from gateway.pipeline.idempotency import Origin, dump_chat, dump_embed, load_chat, load_embed


def test_absent_header_means_no_idempotency() -> None:
    assert Idempotency.from_header(None, caller="key:1", route="/x", body=b"{}") is None


@pytest.mark.parametrize("bad", ["", " leading-space", "tab\tkey", "ü", "x" * 256])
def test_malformed_keys_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidInput):
        Idempotency.from_header(bad, caller="key:1", route="/x", body=b"{}")


def test_the_fingerprint_covers_route_and_body() -> None:
    def fp(route: str, body: bytes) -> bytes:
        idem = Idempotency.from_header("k-1", caller="key:1", route=route, body=body)
        assert idem is not None
        return idem.fingerprint

    assert fp("/v1/chat/completions", b"{}") == fp("/v1/chat/completions", b"{}")
    assert fp("/v1/chat/completions", b"{}") != fp("/v1/embeddings", b"{}")
    assert fp("/v1/chat/completions", b"{}") != fp("/v1/chat/completions", b"{ }")


def test_chat_results_replay_exactly() -> None:
    origin = Origin("req-1", datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    response = CanonicalResponse(
        id="cmpl-9",
        model="m",
        content=None,
        tool_calls=(ToolCall("call_1", "lookup", '{"q": "ü"}'),),
        finish_reason="tool_calls",
        usage=Usage(input_tokens=10, output_tokens=4, cached_tokens=2, reasoning_tokens=1),
    )
    replayed, cost, again = load_chat(dump_chat(origin, 123, response))
    assert (replayed, cost) == (response, 123)
    assert again == Origin("req-1", origin.at, replayed=True)


def test_embedding_results_replay_bit_for_bit() -> None:
    origin = Origin("req-2", datetime(2026, 11, 3, tzinfo=UTC))
    response = EmbedResponse(
        model="e", vectors=((0.1, -2.5e-08, 1 / 3), (7.0,)), usage=Usage(input_tokens=3)
    )
    replayed, cost, _ = load_embed(dump_embed(origin, 5, response))
    assert (replayed, cost) == (response, 5)
