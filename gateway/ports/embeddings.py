"""Embeddings (spec section 2: bge-m3, self-hosted, behind the `Embedder` interface)."""

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order, each of EMBEDDING_DIMENSIONS floats."""
        ...
