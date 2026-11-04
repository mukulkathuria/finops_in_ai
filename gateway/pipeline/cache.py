"""The response cache step (spec section 4, step 6; US-18): an exact match first, then a semantic
one, both inside the request's org and permission scope."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from gateway.adapters.postgres.cache import CacheRepository, CacheSettingsCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.policies import PolicyCache
from gateway.domain.cache import (
    CacheKeys,
    CacheSettings,
    cache_keys,
    response_from,
    response_record,
    single_turn_question,
)
from gateway.domain.llm import CanonicalRequest, CanonicalResponse
from gateway.domain.parsing import text_tokens
from gateway.ports.embeddings import Embedder


@dataclass(frozen=True, slots=True)
class Lookup:
    """What looking a request up (and storing its answer) needs."""

    settings: CacheSettings
    keys: CacheKeys
    embedding: list[float] | None  # the question's, for the semantic cache
    embedded_tokens: int


@dataclass(frozen=True, slots=True)
class CacheHit:
    kind: Literal["exact", "semantic"]
    response: CanonicalResponse
    cost: int  # what the provider charged when the answer was made
    similarity: float | None = None


class ResponseCache:
    def __init__(
        self,
        *,
        db: Database,
        repo: CacheRepository,
        settings: CacheSettingsCache,
        policies: PolicyCache,
        embedder: Embedder | None,
    ) -> None:
        self._db = db
        self._repo = repo
        self._settings = settings
        self._policies = policies
        self._embedder = embedder

    async def prepare(
        self, *, org_id: UUID, team_id: UUID | None, user_id: UUID | None, req: CanonicalRequest
    ) -> Lookup | None:
        """The request's keys, and its question's embedding for the semantic cache; None when the
        org's cache is off or a request with tools is not cached."""
        settings = await self._settings.get(org_id)
        if settings.mode == "off" or req.tools:
            return None
        policy = (await self._policies.get(org_id)).to_dict()
        keys = cache_keys(org_id=org_id, team_id=team_id, user_id=user_id, req=req, policy=policy)
        question = single_turn_question(req) if settings.mode == "semantic" else None
        if question is None or self._embedder is None:
            return Lookup(settings, keys, None, 0)
        (embedding,) = await self._embedder.embed([question])
        return Lookup(settings, keys, embedding, text_tokens(question))

    async def find(self, org_id: UUID, lookup: Lookup, *, request_id: str) -> CacheHit | None:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            exact = await self._repo.exact(conn, lookup.keys.exact)
            if exact is not None:
                response = response_from(exact.response, response_id=request_id)
                return CacheHit("exact", response, exact.cost)
            if lookup.embedding is None or self._embedder is None:
                return None
            near = await self._repo.nearest(
                conn, lookup.keys.partition, lookup.embedding, self._embedder.model
            )
        if near is None or near.similarity is None or near.similarity < lookup.settings.threshold:
            return None
        response = response_from(near.response, response_id=request_id)
        return CacheHit("semantic", response, near.cost, near.similarity)

    async def store(
        self, org_id: UUID, lookup: Lookup, response: CanonicalResponse, cost: int
    ) -> None:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._repo.store(
                conn,
                key=lookup.keys.exact,
                partition=lookup.keys.partition,
                response=response_record(response),
                cost=cost,
                embedding=lookup.embedding,
                embedding_model=self._embedder.model if self._embedder else None,
                ttl_s=lookup.settings.ttl_s,
            )
