"""Price sync (Phase 3): the list is validated as a whole; per model the entry in effect now is
compared with the catalog and later ones are scheduled; a broken list cannot make a paid model
free, move a price tenfold or change a model's kind."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from gateway.domain.price_list import PriceEntry, SyncPlan, plan_sync, suspicious
from gateway.domain.pricing import ModelPrice
from worker.jobs.price_sync import parse_price_list, source_label

SHIPPED = Path(__file__).parents[2] / "config" / "prices.json"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
OPUS = ModelPrice(
    7,
    "anthropic",
    "claude-opus-5-5",
    "chat",
    4_000_000,
    20_000_000,
    200_000,
    5_000_000,
    context_window=1_000_000,
    max_output_tokens=128_000,
)
CURRENT = {("anthropic", "claude-opus-5-5"): OPUS}


def _entry(at: datetime | None = None, **changes: Any) -> PriceEntry:
    return PriceEntry(replace(OPUS, catalog_version=0, **changes), at)


def test_an_unchanged_model_is_left_alone_and_a_change_is_a_new_row() -> None:
    assert plan_sync([_entry()], CURRENT, NOW) == SyncPlan((), (), 1)
    changed = _entry(price_output=22_000_000)
    assert plan_sync([changed], CURRENT, NOW).inserts == (changed,)
    new = PriceEntry(replace(OPUS, catalog_version=0, model="claude-next"))
    assert plan_sync([new], CURRENT, NOW).inserts == (new,)


def test_the_entry_in_effect_now_is_compared_and_later_ones_are_scheduled() -> None:
    earlier = _entry(NOW - timedelta(days=1), price_output=22_000_000)
    later = _entry(NOW + timedelta(days=30), price_output=24_000_000)
    plan = plan_sync([_entry(), earlier, later], CURRENT, NOW)
    assert plan.inserts == (replace(earlier, effective_from=None), later)  # the dated one wins
    # Once the later price is current, the list is in step: nothing to do.
    current = {("anthropic", "claude-opus-5-5"): replace(OPUS, price_output=24_000_000)}
    in_step = plan_sync([_entry(), earlier, later], current, NOW + timedelta(days=31))
    assert in_step == SyncPlan((), (), 1)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"price_input": 0}, "price_input would drop to zero"),
        ({"price_output": 200_000_001}, "price_output would move more than 10x"),
        ({"price_cached_input": 19_999}, "price_cached_input would move more than 10x"),
        ({"kind": "embedding"}, "the kind of model would change"),
    ],
)
def test_suspicious_changes_are_refused(changes: dict[str, Any], reason: str) -> None:
    entry = _entry(**changes)
    plan = plan_sync([entry], CURRENT, NOW)
    assert plan.refused == ((entry, reason),)
    assert plan.inserts == ()


IMAGE = ModelPrice(
    9,
    "openai",
    "dall-e-3",
    "image",
    0,
    0,
    price_image=40_000,
    image_prices=(("1024x1024/hd", 80_000), ("1024x1024/standard", 40_000)),
)
FLAT = replace(IMAGE, image_prices=())


@pytest.mark.parametrize(
    ("old", "table", "reason"),
    [
        (IMAGE, {"1024x1024/hd": 0}, "image option 1024x1024/hd would be free"),
        (IMAGE, {"1024x1024/hd": 7_999}, "image option 1024x1024/hd would move more than 10x"),
        (IMAGE, {"4096x4096/hd": 0}, "image option 4096x4096/hd would be free"),
        # A table in place of a flat price is compared on the default image it priced.
        (
            FLAT,
            {"1024x1024/standard": 400},
            "image option 1024x1024/standard would move more than 10x",
        ),
        (IMAGE, {"1024x1024/hd": 120_000, "1792x1024/hd": 120_000}, None),
    ],
)
def test_each_image_option_is_held_to_the_same_rules(
    old: ModelPrice, table: dict[str, int], reason: str | None
) -> None:
    """Phase 5 review C-I2: a broken list cannot make an image option free or move it tenfold."""
    prices = dict(old.image_prices) | table
    new = replace(old, image_prices=tuple(sorted(prices.items())))
    assert suspicious(old, new) == reason


def test_a_new_image_model_priced_by_its_table_alone_is_accepted() -> None:
    table_only = PriceEntry(
        replace(IMAGE, catalog_version=0, model="gpt-image-2", price_image=None)
    )
    assert plan_sync([table_only], {}, NOW).inserts == (table_only,)


def test_dropping_an_optional_price_falls_back_to_the_base_rate() -> None:
    assert suspicious(OPUS, replace(OPUS, price_cached_input=None)) is None
    image = replace(OPUS, kind="image", price_image=40_000)
    assert suspicious(image, replace(image, price_image=None)) == "price_image would drop to zero"


def _listed(**changes: Any) -> bytes:
    entry = {
        "provider": "anthropic",
        "model": "m",
        "kind": "chat",
        "price_input": 1,
        "price_output": 2,
        "context_window": 10,
        "max_output_tokens": 5,
        **changes,
    }
    return orjson.dumps({"models": [entry]})


def test_a_price_list_is_validated_as_a_whole() -> None:
    (entry,) = parse_price_list(_listed(effective_from="2027-01-01T00:00:00Z"))
    assert entry.effective_from == datetime(2027, 1, 1, tzinfo=UTC)
    assert (entry.price.price_input, entry.price.catalog_version) == (1, 0)
    twice = orjson.dumps({"models": [orjson.loads(_listed())["models"][0]] * 2})
    for bad in (
        _listed(price_input=-1),
        _listed(provider="Bad Name"),
        _listed(colour="red"),
        _listed(effective_from="2027-01-01T00:00:00"),  # no time zone
        twice,
        b"not json",
    ):
        with pytest.raises(ValueError, match="invalid price list"):
            parse_price_list(bad)


def test_the_shipped_list_is_valid() -> None:
    entries = parse_price_list(SHIPPED.read_bytes())
    assert {e.price.provider for e in entries} == {"anthropic"}


def test_a_url_is_recorded_without_credentials_or_query() -> None:
    url = "https://ops:" + "pw" + "@prices.example.com/v1/list.json?token=t"
    assert source_label(url) == "sync:https://prices.example.com/v1/list.json"
    assert source_label("config/prices.json") == "sync:prices.json"
