"""Helpers shared by the HTTP adapters: SSE framing, error bodies, Retry-After parsing."""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import orjson

from gateway.domain.llm import ProviderError, ProviderErrorKind


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the `data` payload of each server-sent event (multi-line data joined with \\n)."""
    buffer: list[str] = []
    async for line in lines:
        if line == "":
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            buffer.append(value[1:] if value.startswith(" ") else value)
    if buffer:
        yield "\n".join(buffer)


def json_body(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = orjson.loads(response.content)
    except orjson.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


def transport_error(exc: BaseException, provider: str) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx2.TimeoutException):
        return ProviderError(ProviderErrorKind.TIMEOUT, "provider timed out", provider=provider)
    if isinstance(exc, httpx2.HTTPError):
        return ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            f"provider unreachable ({type(exc).__name__})",
            provider=provider,
        )
    return ProviderError(
        ProviderErrorKind.OTHER, f"provider call failed ({type(exc).__name__})", provider=provider
    )


def malformed(provider: str) -> ProviderError:
    return ProviderError(
        ProviderErrorKind.OTHER, "malformed response from provider", provider=provider
    )
