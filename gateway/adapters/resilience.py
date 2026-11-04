"""Shared resilience helpers for calls to sidecars and model servers."""

import time
from collections.abc import Callable


class CircuitBreaker:
    """Opens after `threshold` failures in a row and stays open for `cooldown_s`."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self._open_until

    def success(self) -> None:
        self._failures = 0

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown
            self._failures = 0
