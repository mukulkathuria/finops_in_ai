"""Budget rules: scope shapes, limits, worst-case demand and the prompt-token upper bound."""

from uuid import uuid7

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gateway.domain.budgets import (
    EMBED_INPUT_OVERHEAD,
    Budget,
    BudgetChange,
    BudgetStatus,
    Demand,
    chat_demand,
    embed_demand,
    validate_limit,
)
from gateway.domain.errors import InvalidInput
from gateway.domain.llm import (
    PER_IMAGE_TOKENS,
    PER_MESSAGE_OVERHEAD,
    PER_TOOL_OVERHEAD,
    CanonicalRequest,
    EmbedRequest,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolSpec,
    upper_bound_input_tokens,
)
from gateway.domain.pricing import ModelPrice, per_million

TEAM, USER = uuid7(), uuid7()
PRICE = ModelPrice(1, "p", "m", "chat", price_input=1_000_000, price_output=2_000_000)


def _req(
    *messages: Message, max_tokens: int = 100, tools: tuple[ToolSpec, ...] = ()
) -> CanonicalRequest:
    return CanonicalRequest(model="m", messages=messages, max_tokens=max_tokens, tools=tools)


def test_demand_by_unit() -> None:
    demand = Demand(usd_micro=1234, tokens=99, images=2)
    assert (demand.amount("usd"), demand.amount("tokens"), demand.amount("images")) == (1234, 99, 2)


@pytest.mark.parametrize("bad", [-1, 10**15 + 1])
def test_limits_are_bounded(bad: int) -> None:
    with pytest.raises(InvalidInput):
        validate_limit(bad)


@pytest.mark.parametrize(
    ("scope", "team", "user"),
    [("org", TEAM, None), ("team", None, None), ("team", TEAM, USER), ("member", TEAM, None)],
)
def test_scope_shapes_are_enforced(scope: str, team: object, user: object) -> None:
    with pytest.raises(InvalidInput):
        BudgetChange(scope, team, user, "month", "usd", 10)  # type: ignore[arg-type]


def test_valid_changes_and_soft_threshold_bounds() -> None:
    BudgetChange("member", TEAM, USER, "day", "tokens", 0, soft_pct=100)
    with pytest.raises(InvalidInput):
        BudgetChange("org", None, None, "day", "tokens", 10, soft_pct=0)


def test_remaining_counts_grants_and_never_goes_negative() -> None:
    budget = Budget(uuid7(), "team", TEAM, None, "month", "usd", 100, 80)
    status = BudgetStatus(budget, period_start=None, committed=70, reserved=20, granted=50)  # type: ignore[arg-type]
    assert (status.effective_limit, status.remaining) == (150, 60)
    over = BudgetStatus(budget, period_start=None, committed=130, reserved=0, granted=0)  # type: ignore[arg-type]
    assert over.remaining == 0


def test_prompt_bound_counts_bytes_and_fixed_allowances() -> None:
    req = _req(
        Message("system", (TextPart("héllo"),)),  # 6 bytes in UTF-8
        Message("user", (TextPart("hi"), ImagePart("https://x/y.png"))),
        Message("assistant", tool_calls=(ToolCall("c1", "f", "{}"),)),
        Message("tool", (TextPart("ok"),), tool_call_id="c1"),
        tools=(ToolSpec("f", "d", {"type": "object"}),),
    )
    expected = (
        6
        + 2
        + PER_IMAGE_TOKENS
        + (2 + 1 + 2)
        + (2 + 2)
        + 4 * PER_MESSAGE_OVERHEAD
        + PER_TOOL_OVERHEAD
        + 1
        + 1
        + len('{"type":"object"}')
    )
    assert upper_bound_input_tokens(req) == expected


@given(st.text(max_size=400), st.integers(1, 4096))
def test_chat_demand_bounds_any_byte_level_tokenization(text: str, max_tokens: int) -> None:
    demand = chat_demand(PRICE, _req(Message("user", (TextPart(text),)), max_tokens=max_tokens))
    prompt = len(text.encode()) + PER_MESSAGE_OVERHEAD
    assert demand.tokens == prompt + max_tokens
    assert demand.usd_micro >= per_million(prompt, 1_000_000) + per_million(max_tokens, 2_000_000)


def test_embed_demand_covers_every_input() -> None:
    demand = embed_demand(PRICE, EmbedRequest("m", ("alpha", "βeta")))
    assert demand.tokens == 5 + 5 + 2 * EMBED_INPUT_OVERHEAD
    assert demand.usd_micro >= per_million(demand.tokens, 1_000_000)
