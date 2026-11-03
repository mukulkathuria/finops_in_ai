"""Pricing math (ADR 0003): integer micro-USD, ceil rounding, tiers, catalog aliases."""

from hypothesis import given
from hypothesis import strategies as st

from gateway.domain.llm import Usage
from gateway.domain.pricing import Catalog, ModelPrice, per_million, usd

OPUS = ModelPrice(
    1, "anthropic", "claude-opus-5-5", "chat", 4_000_000, 20_000_000, 200_000, 5_000_000
)
HAIKU = ModelPrice(
    2,
    "anthropic",
    "claude-haiku-5-5",
    "chat",
    100_000,
    500_000,
    None,
    125_000,
    None,
    100_000,
    500_000,
    2_500_000,
)


def test_per_million_rounds_up_and_is_exact() -> None:
    assert per_million(1, 150_000) == 1  # 0.15 micro-USD rounds up to 1
    assert per_million(1_000_000, 2_500_000) == 2_500_000
    assert per_million(0, 9_999_999) == 0


def test_usd_formatting_has_no_floats() -> None:
    assert usd(1_234_567) == "1.234567"
    assert usd(5) == "0.000005"


def test_cost_splits_plain_cached_write_and_output_tokens() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cached_tokens=600_000,
        cache_write_tokens=100_000,
    )
    expected = (
        per_million(300_000, 4_000_000)
        + per_million(600_000, 200_000)
        + per_million(100_000, 5_000_000)
        + per_million(100_000, 20_000_000)
    )
    assert OPUS.cost(usage) == expected == 1_200_000 + 120_000 + 500_000 + 2_000_000


def test_long_prompt_tier_prices_the_whole_request() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1_000)
    long = Usage(input_tokens=100_001, output_tokens=1_000)
    assert HAIKU.cost(short) == per_million(100_000, 100_000) + per_million(1_000, 500_000)
    assert HAIKU.cost(long) == per_million(100_001, 500_000) + per_million(1_000, 2_500_000)


@given(st.integers(0, 2_000_000), st.integers(0, 200_000), st.integers(0, 100), st.integers(0, 100))
def test_worst_case_bounds_every_possible_actual_cost(
    inp: int, out: int, cached_pct: int, write_pct: int
) -> None:
    cached = inp * cached_pct // 100
    write = (inp - cached) * write_pct // 100
    usage = Usage(
        input_tokens=inp, output_tokens=out, cached_tokens=cached, cache_write_tokens=write
    )
    for price in (OPUS, HAIKU):
        assert 0 <= price.cost(usage) <= price.worst_case(input_tokens=inp, max_output_tokens=out)


@given(st.integers(0, 10**9), st.integers(0, 10**9), st.integers(0, 10**8))
def test_cost_is_monotonic_in_tokens(a: int, b: int, price: int) -> None:
    lo, hi = sorted((a, b))
    assert 0 <= per_million(lo, price) <= per_million(hi, price)


def test_catalog_resolves_public_names_and_unambiguous_aliases() -> None:
    clash = ModelPrice(3, "other", "claude-opus-5-5", "chat", 1, 1)
    catalog = Catalog([OPUS, HAIKU, clash])
    assert catalog.resolve("anthropic/claude-opus-5-5") is OPUS
    assert catalog.resolve("claude-haiku-5-5") is HAIKU
    assert catalog.resolve("claude-opus-5-5") is None  # ambiguous bare name
    assert catalog.resolve("nope") is None
    assert next(m.public_name for m in catalog.models("chat")) == "anthropic/claude-haiku-5-5"


BULK = ModelPrice(
    9,
    "p",
    "bulk",
    "chat",
    price_input=1_000_000,
    price_output=4_000_000,
    tier_threshold_tokens=1_000,
    tier_price_input=500_000,  # a volume discount: dearer below the threshold than above it
    tier_price_output=2_000_000,
)


@given(st.integers(0, 5_000), st.integers(0, 5_000), st.integers(0, 5_000), st.integers(0, 5_000))
def test_worst_case_bounds_any_smaller_actual_usage(a: int, b: int, c: int, d: int) -> None:
    usage = Usage(input_tokens=min(a, b), output_tokens=min(c, d))
    for price in (OPUS, HAIKU, BULK):
        bound = price.worst_case(input_tokens=max(a, b), max_output_tokens=max(c, d))
        assert price.cost(usage) <= bound
