"""Counter store port: shared fixed-window counters for rate limits (spec section 2, 10)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    hits: int
    limit: int
    retry_after_s: int


class CounterStore(Protocol):
    async def hit(self, key: str, *, limit: int, window_s: int) -> RateDecision:
        """Count one event for `key` in the current window and say whether it is within `limit`."""
        ...
