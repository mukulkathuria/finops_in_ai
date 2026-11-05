"""LLM provider port (spec section 3, family A): adapters implement it, callers depend on it."""

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    EmbedRequest,
    EmbedResponse,
    ImageRequest,
    ImageResponse,
    ModelCapabilities,
    ProviderError,
    Usage,
)


class LLMAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse: ...

    def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        """Yields content and tool-call deltas, then one chunk with finish_reason and usage."""
        ...

    async def embed(self, req: EmbedRequest) -> EmbedResponse: ...

    async def generate_images(self, req: ImageRequest) -> ImageResponse:
        """Images as base64 data; ProviderError(BAD_REQUEST) where the provider has none."""
        ...

    def capabilities(self, model: str) -> ModelCapabilities: ...

    def normalize_usage(self, raw: Mapping[str, Any]) -> Usage: ...

    def classify_error(self, exc: BaseException) -> ProviderError: ...
