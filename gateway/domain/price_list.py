"""Price sync rules (spec section 3, "Model and price registry"): the catalog follows a price list,
as data. For each model the list's entry in effect now is compared with the catalog's current row:
a change becomes a new row (rows are never updated), and an entry dated later is scheduled for its
time. A model the list leaves out keeps its rows. A change that would make a paid model free, move
a price more than tenfold or change the kind of model is refused: a broken list must not switch
budgets off. A scheduled row is fixed; correct it with another entry dated later.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

from gateway.domain.images import DEFAULT_QUALITY, DEFAULT_SIZE
from gateway.domain.pricing import ModelPrice

MAX_FACTOR: Final = 10  # how far one sync may move a price
_COMPARED: Final = (
    "kind",
    "price_input",
    "price_output",
    "price_cached_input",
    "price_cache_write",
    "price_image",
    "tier_threshold_tokens",
    "tier_price_input",
    "tier_price_output",
    "context_window",
    "max_output_tokens",
    "image_prices",
)
_PRICES: Final = (
    "price_input",
    "price_output",
    "price_cached_input",
    "price_cache_write",
    "price_image",
    "tier_price_input",
    "tier_price_output",
)
_FREE_WHEN_UNSET: Final = frozenset({"price_image"})  # without an image price, images are free
_EPOCH: Final = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PriceEntry:
    price: ModelPrice  # its catalog_version is not known yet: 0
    effective_from: datetime | None = None  # None: at once


@dataclass(frozen=True, slots=True)
class SyncPlan:
    inserts: tuple[PriceEntry, ...]
    refused: tuple[tuple[PriceEntry, str], ...]
    unchanged: int


def _same(a: ModelPrice, b: ModelPrice) -> bool:
    return all(getattr(a, name) == getattr(b, name) for name in _COMPARED)


def suspicious(old: ModelPrice, new: ModelPrice) -> str | None:
    """Why a change looks like a broken list rather than a new price; None when it does not."""
    if old.kind != new.kind:
        return "the kind of model would change"
    for name in _PRICES:
        before, after = getattr(old, name), getattr(new, name)
        if after is None and name in _FREE_WHEN_UNSET:
            after = 0
        if not before or after is None:
            continue  # nothing was paid before, or the base rate applies again
        if after == 0:
            return f"{name} would drop to zero"
        if max(before, after) > MAX_FACTOR * min(before, after):
            return f"{name} would move more than {MAX_FACTOR}x"
    return _suspicious_images(old, new)


def _image_prices(entry: ModelPrice) -> dict[str, int]:
    """What each image option costs: the model's table, or its flat price for the default
    image (`image_option`), so a switch between the two is compared like any change."""
    if entry.image_prices:
        return dict(entry.image_prices)
    if entry.price_image is not None:
        return {f"{DEFAULT_SIZE}/{DEFAULT_QUALITY}": entry.price_image}
    return {}


def _suspicious_images(old: ModelPrice, new: ModelPrice) -> str | None:
    """The same rules per image option: a paid option must not become free or move more than
    tenfold, and a model that charges for images must not gain a free option. An option the
    list drops is refused to requests, which costs no money."""
    before = _image_prices(old)
    paid = any(before.values())
    for option, after in sorted(_image_prices(new).items()):
        was = before.get(option)
        if after == 0 and (was or (was is None and paid)):
            return f"image option {option} would be free"
        if was and max(was, after) > MAX_FACTOR * min(was, after):
            return f"image option {option} would move more than {MAX_FACTOR}x"
    return None


def unpriced(new: ModelPrice) -> str | None:
    """Why a model new to the catalog looks like a broken entry; None when it does not."""
    if not any(getattr(new, name) for name in _PRICES) and not any(_image_prices(new).values()):
        return "a new model would have no price at all"
    return None


def plan_sync(
    entries: Sequence[PriceEntry], current: Mapping[tuple[str, str], ModelPrice], now: datetime
) -> SyncPlan:
    """The rows a sync appends: per model, the list's entry in effect now when it differs from
    the current row, and every entry dated later. Suspicious ones are refused instead."""
    by_model: dict[tuple[str, str], list[PriceEntry]] = {}
    for entry in entries:
        by_model.setdefault((entry.price.provider, entry.price.model), []).append(entry)
    inserts: list[PriceEntry] = []
    refused: list[tuple[PriceEntry, str]] = []
    unchanged = 0
    for key, group in by_model.items():
        old = current.get(key)
        due = [e for e in group if e.effective_from is None or e.effective_from <= now]
        pending: list[PriceEntry] = []
        if due:
            entry = max(due, key=lambda e: e.effective_from or _EPOCH)
            if old is not None and _same(old, entry.price):
                unchanged += 1
            else:
                pending.append(replace(entry, effective_from=None))  # in effect at once
        pending += [e for e in group if e.effective_from is not None and e.effective_from > now]
        for entry in pending:
            reason = suspicious(old, entry.price) if old is not None else unpriced(entry.price)
            if reason is None:
                inserts.append(entry)
            else:
                refused.append((entry, reason))
    return SyncPlan(tuple(inserts), tuple(refused), unchanged)
