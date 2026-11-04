"""Cached answers (US-18): exact lookups by key and semantic lookups by meaning inside a partition,
all under row-level security. Expired entries are purged now and then as answers are stored."""

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_one
from gateway.domain.cache import CacheSettings

_SETTINGS: Final = "SELECT settings -> 'cache' AS cache FROM orgs WHERE id = app_current_org()"
_SETTINGS_LOCKED: Final = _SETTINGS + " FOR UPDATE"
_SET_SETTINGS: Final = (
    "UPDATE orgs SET settings = jsonb_set(settings, '{cache}', CAST(:cache AS jsonb))"
    " WHERE id = app_current_org() RETURNING id"
)
_EXACT: Final = (
    "SELECT response, cost_micro_usd FROM cache_entries"
    " WHERE key_hash = :key AND expires_at > now()"
)
_TUNE: Final = ("SET LOCAL hnsw.iterative_scan = relaxed_order", "SET LOCAL hnsw.ef_search = 40")
_NEAREST: Final = """
SELECT response, cost_micro_usd, 1 - (embedding <=> CAST(:query AS vector)) AS similarity
FROM cache_entries
WHERE partition_hash = :partition AND embedding_model = :model AND expires_at > now()
ORDER BY embedding <=> CAST(:query AS vector)
LIMIT 1
"""
_STORE: Final = """
INSERT INTO cache_entries (org_id, key_hash, partition_hash, response, cost_micro_usd, embedding,
  embedding_model, expires_at)
VALUES (app_current_org(), :key, :partition, CAST(:response AS jsonb), :cost,
  CAST(:embedding AS vector), :embedding_model,
  now() + make_interval(secs => CAST(:ttl AS double precision)))
ON CONFLICT (org_id, key_hash) DO UPDATE
  SET response = EXCLUDED.response, cost_micro_usd = EXCLUDED.cost_micro_usd,
      embedding = EXCLUDED.embedding, embedding_model = EXCLUDED.embedding_model,
      expires_at = EXCLUDED.expires_at
"""
_PURGE: Final = "DELETE FROM cache_entries WHERE expires_at < now()"
_PURGE_PROBABILITY: Final = 0.01


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    response: dict[str, Any]
    cost: int
    similarity: float | None = None


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(format(v, ".7g") for v in values) + "]"


class CacheRepository:
    async def settings(self, conn: AsyncConnection, *, lock: bool = False) -> CacheSettings:
        row = await fetch_one(conn, _SETTINGS_LOCKED if lock else _SETTINGS)
        raw = row["cache"] if row is not None else None
        return CacheSettings.parse(raw if isinstance(raw, dict) else None)

    async def set_settings(self, conn: AsyncConnection, settings: CacheSettings) -> None:
        await fetch_one(conn, _SET_SETTINGS, {"cache": orjson.dumps(settings.to_dict()).decode()})

    async def exact(self, conn: AsyncConnection, key: bytes) -> CachedAnswer | None:
        row = await fetch_one(conn, _EXACT, {"key": key})
        return CachedAnswer(dict(row["response"]), int(row["cost_micro_usd"])) if row else None

    async def nearest(
        self, conn: AsyncConnection, partition: bytes, query: Sequence[float], model: str
    ) -> CachedAnswer | None:
        for statement in _TUNE:
            await conn.execute(text(statement))
        row = await fetch_one(
            conn, _NEAREST, {"partition": partition, "query": _vector(query), "model": model}
        )
        if row is None:
            return None
        return CachedAnswer(
            dict(row["response"]), int(row["cost_micro_usd"]), float(row["similarity"])
        )

    async def store(
        self,
        conn: AsyncConnection,
        *,
        key: bytes,
        partition: bytes,
        response: dict[str, Any],
        cost: int,
        embedding: Sequence[float] | None,
        embedding_model: str | None,
        ttl_s: int,
    ) -> None:
        await conn.execute(
            text(_STORE),
            {
                "key": key,
                "partition": partition,
                "response": orjson.dumps(response).decode(),
                "cost": cost,
                "embedding": _vector(embedding) if embedding is not None else None,
                "embedding_model": embedding_model if embedding is not None else None,
                "ttl": ttl_s,
            },
        )
        # Sampling for housekeeping, not security.
        if random.random() < _PURGE_PROBABILITY:  # noqa: S311  # nosec B311
            await conn.execute(text(_PURGE))


class CacheSettingsCache:
    """Each org's cache settings as snapshots (the `PolicyCache` pattern)."""

    def __init__(
        self,
        db: Database,
        repo: CacheRepository,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, CacheSettings]] = {}

    async def get(self, org_id: UUID) -> CacheSettings:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            settings = await self._repo.settings(conn)
        self._entries[org_id] = (self._clock() + self._ttl, settings)
        return settings

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
