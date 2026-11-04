"""Rerankers (spec section 2: bge-reranker, self-hosted, behind the `Reranker` interface)."""

from collections.abc import Sequence
from typing import Protocol


class Reranker(Protocol):
    @property
    def model(self) -> str: ...

    async def rerank(self, query: str, passages: Sequence[str]) -> list[float]:
        """One relevance score per passage, in order: higher is more relevant."""
        ...
