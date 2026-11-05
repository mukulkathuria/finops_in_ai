"""Prices and cost (ADR 0003): integer micro-USD, prices per million tokens, ceil per component."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from gateway.domain.llm import Usage

MICRO_USD_PER_USD: Final = 1_000_000
# cost() rounds four token components up separately; worst_case() rounds two. Each extra ceil can
# add at most one micro-USD, so the bound carries this slack to stay a true upper bound.
ROUNDING_SLACK: Final = 3
ModelKind = Literal["chat", "embedding", "image"]


def per_million(tokens: int, price_per_million: int) -> int:
    """ceil(tokens * price / 1e6) in exact integer arithmetic: never under-charges."""
    return -(-tokens * price_per_million // 1_000_000)


def usd(micro_usd: int) -> str:
    sign = "-" if micro_usd < 0 else ""
    whole, frac = divmod(abs(micro_usd), MICRO_USD_PER_USD)
    return f"{sign}{whole}.{frac:06d}"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    catalog_version: int
    provider: str
    model: str
    kind: ModelKind
    price_input: int
    price_output: int
    price_cached_input: int | None = None
    price_cache_write: int | None = None
    price_image: int | None = None
    tier_threshold_tokens: int | None = None
    tier_price_input: int | None = None
    tier_price_output: int | None = None
    context_window: int = 0
    max_output_tokens: int = 0
    image_prices: tuple[tuple[str, int], ...] = ()  # "WxH/quality": micro-USD per image

    @property
    def public_name(self) -> str:
        return f"{self.provider}/{self.model}"

    def _rates(self, input_tokens: int) -> tuple[int, int]:
        if self.tier_threshold_tokens is not None and input_tokens > self.tier_threshold_tokens:
            return (
                self.tier_price_input or self.price_input,
                self.tier_price_output or self.price_output,
            )
        return self.price_input, self.price_output

    def cost(self, usage: Usage, *, images: int = 0) -> int:
        rate_in, rate_out = self._rates(usage.input_tokens)
        cached_rate = rate_in if self.price_cached_input is None else self.price_cached_input
        write_rate = rate_in if self.price_cache_write is None else self.price_cache_write
        plain = usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens
        return (
            per_million(plain, rate_in)
            + per_million(usage.cached_tokens, cached_rate)
            + per_million(usage.cache_write_tokens, write_rate)
            + per_million(usage.output_tokens, rate_out)
            + images * (self.price_image or 0)
        )

    def worst_case(self, *, input_tokens: int, max_output_tokens: int, images: int = 0) -> int:
        """Upper bound for a reservation, valid for any actual usage up to these counts: every
        input token at the dearest input rate (a cache write can cost more than a plain read) and
        every allowed output token. Above a tier threshold the actual prompt may land on either
        side of it, so the dearer of the two rates applies."""
        rate_in, rate_out = self.price_input, self.price_output
        if self.tier_threshold_tokens is not None and input_tokens > self.tier_threshold_tokens:
            rate_in = max(rate_in, self.tier_price_input or 0)
            rate_out = max(rate_out, self.tier_price_output or 0)
        dearest_in = max(rate_in, self.price_cache_write or 0, self.price_cached_input or 0)
        return (
            per_million(input_tokens, dearest_in)
            + per_million(max_output_tokens, rate_out)
            + images * (self.price_image or 0)
            + ROUNDING_SLACK
        )


class Catalog:
    """Immutable snapshot of current prices, swapped atomically when refreshed (ADR 0005)."""

    def __init__(self, entries: Iterable[ModelPrice]) -> None:
        self._by_public = {e.public_name: e for e in entries}
        by_model: dict[str, list[ModelPrice]] = {}
        for e in self._by_public.values():
            by_model.setdefault(e.model, []).append(e)
        # A bare model name is an alias only while it is unambiguous across providers.
        self._aliases = {name: found[0] for name, found in by_model.items() if len(found) == 1}

    def resolve(self, name: str) -> ModelPrice | None:
        return self._by_public.get(name) or self._aliases.get(name)

    def models(self, kind: ModelKind | None = None) -> list[ModelPrice]:
        return sorted(
            (e for e in self._by_public.values() if kind is None or e.kind == kind),
            key=lambda e: e.public_name,
        )
