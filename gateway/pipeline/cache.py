"""The response cache step (spec section 4, step 6; US-18): an exact match first, then a semantic
one, both inside the request's org and permission scope and keyed by the guardrail policy that
inspected the request. Answers are stored sealed (envelope encryption bound to the org and the
entry's key; spec section 5, "Retention and privacy"); one that cannot be opened is a miss."""

import logging
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

import orjson

from gateway.adapters.postgres.cache import CachedAnswer, CacheRepository, CacheSettingsCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.security.envelope import DecryptionError, Envelope, Sealed
from gateway.domain.cache import (
    CacheKeys,
    CacheSettings,
    cache_keys,
    image_cache_keys,
    response_from,
    response_record,
    single_turn_question,
)
from gateway.domain.guardrails import GuardrailPolicy
from gateway.domain.llm import CanonicalRequest, CanonicalResponse, ImageRequest, ImageResponse
from gateway.domain.parsing import text_tokens
from gateway.ports.embeddings import Embedder

log = logging.getLogger(__name__)

# The janitor caps entries per org; this caps an image answer's size, so the two bound the bytes
# an org's cached images take (a chat answer is far smaller).
MAX_CACHED_IMAGE_BYTES: Final = 8 * 1024 * 1024  # of base64 text


def _aad(org_id: UUID, key: bytes) -> bytes:
    """What a sealed answer is bound to: its org and its exact key."""
    return b"finops:cache:" + str(org_id).encode() + b":" + key.hex().encode()


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
        envelope: Envelope,
        embedder: Embedder | None,
    ) -> None:
        self._db = db
        self._repo = repo
        self._settings = settings
        self._envelope = envelope
        self._embedder = embedder

    async def provider_cache(self, org_id: UUID) -> bool:
        """Whether the org lets requests mark their stable prefix for the provider's cache."""
        return (await self._settings.get(org_id)).provider_cache

    async def prepare(
        self,
        *,
        org_id: UUID,
        team_id: UUID | None,
        user_id: UUID | None,
        req: CanonicalRequest,
        policy: GuardrailPolicy,
    ) -> Lookup | None:
        """The request's keys, and its question's embedding for the semantic cache; None when the
        org's cache is off or a request with tools is not cached."""
        settings = await self._settings.get(org_id)
        if settings.mode == "off" or req.tools:
            return None
        keys = cache_keys(
            org_id=org_id, team_id=team_id, user_id=user_id, req=req, policy=policy.to_dict()
        )
        question = single_turn_question(req) if settings.mode == "semantic" else None
        if question is None or self._embedder is None:
            return Lookup(settings, keys, None, 0)
        (embedding,) = await self._embedder.embed([question])
        return Lookup(settings, keys, embedding, text_tokens(question))

    def _record(self, org_id: UUID, answer: CachedAnswer, request_id: str) -> Any:
        sealed = Sealed(answer.ciphertext, answer.wrapped_key, answer.key_id)
        try:
            return orjson.loads(self._envelope.open(sealed, _aad(org_id, answer.key_hash)))
        except DecryptionError, ValueError:
            # A master key no longer configured, or a damaged entry: the answer is gone.
            log.warning(
                "a cached answer could not be opened; a miss", extra={"request_id": request_id}
            )
            return None

    def _open(
        self, org_id: UUID, answer: CachedAnswer, request_id: str
    ) -> CanonicalResponse | None:
        record = self._record(org_id, answer, request_id)
        try:
            return response_from(record, response_id=request_id) if record is not None else None
        except ValueError, KeyError, TypeError:
            log.warning("a cached answer is unreadable; a miss", extra={"request_id": request_id})
            return None

    async def find(self, org_id: UUID, lookup: Lookup, *, request_id: str) -> CacheHit | None:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            exact = await self._repo.exact(conn, lookup.keys.exact)
            near = None
            if exact is None and lookup.embedding is not None and self._embedder is not None:
                near = await self._repo.nearest(
                    conn, lookup.keys.partition, lookup.embedding, self._embedder.model
                )
        if exact is not None:
            response = self._open(org_id, exact, request_id)
            return CacheHit("exact", response, exact.cost) if response is not None else None
        if near is None or near.similarity is None or near.similarity < lookup.settings.threshold:
            return None
        response = self._open(org_id, near, request_id)
        if response is None:
            return None
        return CacheHit("semantic", response, near.cost, near.similarity)

    async def store(
        self, org_id: UUID, lookup: Lookup, response: CanonicalResponse, cost: int
    ) -> None:
        plain = orjson.dumps(response_record(response)).decode()
        sealed = self._envelope.seal(plain, _aad(org_id, lookup.keys.exact))
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._repo.store(
                conn,
                key=lookup.keys.exact,
                partition=lookup.keys.partition,
                ciphertext=sealed.ciphertext,
                wrapped_key=sealed.wrapped_key,
                key_id=sealed.key_id,
                cost=cost,
                embedding=lookup.embedding,
                embedding_model=self._embedder.model if self._embedder else None,
                ttl_s=lookup.settings.ttl_s,
            )

    async def prepare_images(
        self,
        *,
        org_id: UUID,
        team_id: UUID | None,
        user_id: UUID | None,
        req: ImageRequest,
        policy: GuardrailPolicy,
    ) -> Lookup | None:
        """US-24: an image request's exact key; None when the org's cache is off (images have no
        semantic match)."""
        settings = await self._settings.get(org_id)
        if settings.mode == "off":
            return None
        keys = image_cache_keys(
            org_id=org_id, team_id=team_id, user_id=user_id, req=req, policy=policy.to_dict()
        )
        return Lookup(settings, keys, None, 0)

    async def find_images(
        self, org_id: UUID, lookup: Lookup, *, request_id: str
    ) -> tuple[ImageResponse, int] | None:
        async with self._db.transaction(TenantContext(org_id)) as conn:
            exact = await self._repo.exact(conn, lookup.keys.exact)
        record = self._record(org_id, exact, request_id) if exact is not None else None
        if exact is None or not isinstance(record, dict):
            return None
        try:
            response = ImageResponse(
                model=str(record["model"]),
                images=tuple(str(i) for i in record["images"]),
                revised_prompts=tuple(record.get("revised_prompts") or ()),
            )
        except KeyError, TypeError:
            return None
        return (response, exact.cost) if response.images else None

    async def store_images(
        self, org_id: UUID, lookup: Lookup, response: ImageResponse, cost: int
    ) -> None:
        if sum(map(len, response.images)) > MAX_CACHED_IMAGE_BYTES:
            return  # served, not kept
        record = {
            "model": response.model,
            "images": list(response.images),
            "revised_prompts": list(response.revised_prompts),
        }
        sealed = self._envelope.seal(orjson.dumps(record).decode(), _aad(org_id, lookup.keys.exact))
        async with self._db.transaction(TenantContext(org_id)) as conn:
            await self._repo.store(
                conn,
                key=lookup.keys.exact,
                partition=lookup.keys.partition,
                ciphertext=sealed.ciphertext,
                wrapped_key=sealed.wrapped_key,
                key_id=sealed.key_id,
                cost=cost,
                embedding=None,
                embedding_model=None,
                ttl_s=lookup.settings.ttl_s,
            )
