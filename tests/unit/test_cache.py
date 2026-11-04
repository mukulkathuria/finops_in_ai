"""Response cache keys and rules (US-18): a key differs whenever the org, the permission scope, the
model, a parameter, the system prompt, the conversation or the guardrail policy does; only
single-turn text questions are matched by meaning; only complete plain answers are kept."""

from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.domain.cache import (
    CacheSettings,
    cache_keys,
    cacheable,
    response_from,
    response_record,
    single_turn_question,
)
from gateway.domain.llm import (
    CanonicalRequest,
    CanonicalResponse,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolSpec,
    Usage,
)

ORG, TEAM, USER = uuid4(), uuid4(), uuid4()
POLICY = {"pii": "redact", "output": "redact"}


def _req(text: str = "What is the capital of France?", **fields: object) -> CanonicalRequest:
    messages = (Message("system", (TextPart("be brief"),)), Message("user", (TextPart(text),)))
    return CanonicalRequest(**{"model": "m", "messages": messages, "max_tokens": 50, **fields})


def _keys(req: CanonicalRequest, **scope: object) -> bytes:
    parts: dict[str, object] = {"org_id": ORG, "team_id": TEAM, "user_id": USER, "policy": POLICY}
    return cache_keys(req=req, **{**parts, **scope}).exact  # type: ignore[arg-type]


def test_the_same_request_in_the_same_scope_has_the_same_key() -> None:
    assert _keys(_req()) == _keys(_req())
    assert _keys(_req("  What is the capital of France?  ")) == _keys(_req())  # trimmed


@pytest.mark.parametrize(
    "change",
    [
        {"org_id": uuid4()},
        {"team_id": uuid4()},
        {"user_id": uuid4()},
        {"team_id": None},
        {"policy": {"pii": "block", "output": "redact"}},
    ],
)
def test_another_scope_or_policy_never_shares_a_key(change: dict[str, object]) -> None:
    assert _keys(_req(), **change) != _keys(_req())


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "other"},
        {"max_tokens": 51},
        {"temperature": 0.2},
        {"top_p": 0.5},
        {"stop": ("END",)},
        {"seed": 7},
        {"response_format": {"type": "json_object"}},
    ],
)
def test_every_parameter_is_part_of_the_key(fields: dict[str, object]) -> None:
    assert _keys(_req(**fields)) != _keys(_req())


@given(st.uuids(), st.uuids(), st.uuids(), st.uuids())
def test_keys_never_collide_across_scopes(a: UUID, b: UUID, c: UUID, d: UUID) -> None:
    if (a, b) != (c, d):
        assert _keys(_req(), team_id=a, user_id=b) != _keys(_req(), team_id=c, user_id=d)


def test_only_single_turn_text_questions_are_matched_by_meaning() -> None:
    assert single_turn_question(_req()) == "What is the capital of France?"
    history = (
        Message("user", (TextPart("Hi"),)),
        Message("assistant", (TextPart("Hello"),)),
        Message("user", (TextPart("And France?"),)),
    )
    assert single_turn_question(_req(messages=history)) is None
    picture = (Message("user", (TextPart("What is this?"), ImagePart("https://x/y.png"))),)
    assert single_turn_question(_req(messages=picture)) is None
    tool = ToolSpec("lookup", "find a fact", {"type": "object"})
    assert single_turn_question(_req(tools=(tool,))) is None


def test_only_complete_plain_answers_are_kept_and_they_round_trip() -> None:
    usage = Usage(input_tokens=10, output_tokens=5)
    answer = CanonicalResponse("r1", "m", "Paris.", (), "stop", usage)
    assert cacheable(answer)
    assert not cacheable(CanonicalResponse("r1", "m", "Par", (), "length", usage))
    call = ToolCall("c1", "lookup", "{}")
    assert not cacheable(CanonicalResponse("r1", "m", None, (call,), "tool_calls", usage))
    restored = response_from(response_record(answer), response_id="r2")
    assert (restored.id, restored.content, restored.usage) == ("r2", "Paris.", usage)


def test_settings_are_bounded_and_read_leniently() -> None:
    assert CacheSettings.parse(None) == CacheSettings()
    assert CacheSettings.parse({"mode": "exact", "ttl_s": 600}) == CacheSettings("exact", 600)
    assert CacheSettings.parse({"mode": "sometimes"}).mode == "off"
    with pytest.raises(ValueError, match="threshold"):
        CacheSettings("semantic", 600, 0.5)
