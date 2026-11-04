"""Embeddings from an OpenAI-compatible server (spec section 2: bge-m3, self-hosted; TEI, vLLM
and Infinity all serve `/v1/embeddings`). Every vector must have the dimension of the chunks
column and finite values, or the server is treated as unavailable."""

import math
from collections.abc import Sequence
from typing import Final

import httpx2
from pydantic import BaseModel, ValidationError

from gateway.adapters.resilience import CircuitBreaker
from gateway.domain.errors import Unavailable
from gateway.domain.retrieval import EMBEDDING_DIMENSIONS

_BATCH: Final = 64


class _Item(BaseModel):
    index: int
    embedding: list[float]


class _Answer(BaseModel):
    data: list[_Item]


class OpenAICompatEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None,
        http: httpx2.AsyncClient,
        timeout_s: float,
        dimensions: int = EMBEDDING_DIMENSIONS,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.model = model
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = http
        self._timeout = timeout_s
        self._dimensions = dimensions
        self._breaker = breaker or CircuitBreaker()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            vectors += await self._batch(list(texts[start : start + _BATCH]))
        return vectors

    def _unavailable(self) -> Unavailable:
        return Unavailable(
            "the embedding model is unavailable; try again shortly", code="embedder_unavailable"
        )

    async def _batch(self, texts: list[str]) -> list[list[float]]:
        if not self._breaker.allow():
            raise self._unavailable()
        try:
            resp = await self._http.post(
                f"{self._base}/embeddings",
                json={"model": self.model, "input": texts},
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx2.HTTPError as exc:
            self._breaker.failure()
            raise self._unavailable() from exc
        try:
            if resp.status_code != 200:
                raise ValueError(f"status {resp.status_code}")
            items = sorted(_Answer.model_validate_json(resp.content).data, key=lambda i: i.index)
            if [item.index for item in items] != list(range(len(texts))):
                raise ValueError("the vectors do not match the inputs one to one")
            vectors = [item.embedding for item in items]
            if len(vectors) != len(texts) or any(
                len(v) != self._dimensions or not all(map(math.isfinite, v)) for v in vectors
            ):
                raise ValueError(f"expected {len(texts)} vectors of {self._dimensions} floats")
        except (ValueError, ValidationError) as exc:
            self._breaker.failure()
            raise self._unavailable() from exc
        self._breaker.success()
        return vectors
