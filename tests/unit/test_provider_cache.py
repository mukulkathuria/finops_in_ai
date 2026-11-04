"""Provider prompt caching (Phase 3): the org setting defaults on and round-trips, and the
provider's cache is reported as a technique priced on what the request used."""

from gateway.domain.cache import CacheSettings
from gateway.domain.llm import Usage
from gateway.domain.pricing import ModelPrice
from gateway.domain.savings import provider_cache_report

# Micro-USD per 1M tokens: input 3.00, cache reads 0.30, cache writes 3.75, output 15.00.
PRICE = ModelPrice(
    1,
    "anthropic",
    "m",
    "chat",
    3_000_000,
    15_000_000,
    price_cached_input=300_000,
    price_cache_write=3_750_000,
)


def test_reads_save_and_writes_cost_against_the_full_price() -> None:
    read = provider_cache_report(PRICE, Usage(10_000, 100, cached_tokens=9_000))
    assert read is not None
    assert read.cost_delta_micro_usd == -24_300  # 9,000 tokens at 0.30 instead of 3.00
    assert (read.stage, read.phase, read.applied) == ("provider_cache", "post_llm", True)
    assert (read.tokens_before, read.tokens_after) == (0, 0)  # it removes no token
    assert read.decisions == {"read": 9_000, "written": 0}
    written = provider_cache_report(PRICE, Usage(10_000, 100, cache_write_tokens=9_000))
    assert written is not None
    assert written.cost_delta_micro_usd == 6_750  # the write premium, 0.75 on 9,000 tokens
    assert provider_cache_report(PRICE, Usage(10_000, 100)) is None


def test_the_setting_defaults_on_and_round_trips() -> None:
    assert CacheSettings().provider_cache is True
    assert CacheSettings.parse({"mode": "exact"}).provider_cache is True  # stored before it existed
    off = CacheSettings(provider_cache=False)
    assert CacheSettings.parse(off.to_dict()) == off
