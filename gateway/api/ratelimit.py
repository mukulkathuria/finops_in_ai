"""Edge rate limits as route dependencies (spec section 10: per-key and per-IP limits)."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import Request

from gateway.app.settings import Settings
from gateway.domain.errors import RateLimited
from gateway.ports.counters import CounterStore


class _LimiterServices(Protocol):
    @property
    def counters(self) -> CounterStore: ...
    @property
    def settings(self) -> Settings: ...


def client_key(request: Request) -> str:
    """Hash the client address so raw IPs never sit in the counters table."""
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(host.encode()).hexdigest()[:32]


def per_ip_limit(bucket: str) -> Callable[[Request], Awaitable[None]]:
    async def limiter(request: Request) -> None:
        services: _LimiterServices = request.app.state.container
        decision = await services.counters.hit(
            f"{bucket}:{client_key(request)}",
            limit=services.settings.auth_rate_limit_per_minute,
            window_s=60,
        )
        if not decision.allowed:
            raise RateLimited(
                "too many requests from this address; slow down",
                retry_after_s=decision.retry_after_s,
                code="rate_limited",
            )

    return limiter
