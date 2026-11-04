"""Cached answers (US-18): exact lookups by key and semantic lookups by meaning inside a partition,
all under row-level security. Answers are stored sealed (the pipeline encrypts them, bound to the
org and the entry's key). The worker's janitor deletes expired entries and keeps each org under
its cap; it sees ids, orgs and times, never what an entry holds."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import orjson
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one
from gateway.domain.cache import CacheSettings

_SETTINGS: Final = "SELECT settings -> 'cache' AS cache FROM orgs WHERE id = app_current_org()"
_SETTINGS_LOCKED: Final = _SETTINGS + " FOR UPDATE"
_SET_SETTINGS: Final = (
    "UPDATE orgs SET settings = jsonb_set(settings, '{cache}', CAST(:cache AS jsonb))"
    " WHERE id = app_current_org() RETURNING id"
)
_EXACT: Final = (
    "SELECT key_hash, ciphertext, wrapped_key, key_id, cost_micro_usd FROM cache_entries"
    " WHERE key_hash = :key AND expires_at > now()"
)
_TUNE: Final = ("SET LOCAL hnsw.iterative_scan = relaxed_order", "SET LOCAL hnsw.ef_search = 40")
# relaxed_order may return neighbours slightly out of order: re-rank a few before taking one.
_NEAREST: Final = """
WITH near AS MATERIALIZED (
  SELECT key_hash, ciphertext, wrapped_key, key_id, cost_micro_usd,
         embedding <=> CAST(:query AS vector) AS distance
  FROM cache_entries
  WHERE partition_hash = :partition AND embedding_model = :model AND expires_at > now()
  ORDER BY embedding <=> CAST(:query AS vector)
  LIMIT 5
)
SELECT key_hash, ciphertext, wrapped_key, key_id, cost_micro_usd, 1 - distance AS similarity
FROM near ORDER BY distance LIMIT 1
"""
_STORE: Final = """
INSERT INTO cache_entries (org_id, key_hash, partition_hash, ciphertext, wrapped_key, key_id,
  cost_micro_usd, embedding, embedding_model, expires_at)
VALUES (app_current_org(), :key, :partition, :ciphertext, :wrapped_key, :key_id, :cost,
  CAST(:embedding AS vector), :embedding_model,
  now() + make_interval(secs => CAST(:ttl AS double precision)))
ON CONFLICT (org_id, key_hash) DO UPDATE
  SET ciphertext = EXCLUDED.ciphertext, wrapped_key = EXCLUDED.wrapped_key,
      key_id = EXCLUDED.key_id, cost_micro_usd = EXCLUDED.cost_micro_usd,
      embedding = EXCLUDED.embedding, embedding_model = EXCLUDED.embedding_model,
      expires_at = EXCLUDED.expires_at, created_at = now()
"""
_PURGE: Final = """
DELETE FROM cache_entries WHERE id IN (
  SELECT id FROM cache_entries WHERE expires_at < now() ORDER BY expires_at LIMIT :batch)
"""
_OVER_CAP: Final = """
SELECT org_id, count(*) - :cap AS excess FROM cache_entries
GROUP BY org_id HAVING count(*) > :cap
"""
_EVICT: Final = """
DELETE FROM cache_entries WHERE id IN (
  SELECT id FROM cache_entries WHERE org_id = :org ORDER BY created_at LIMIT :n)
"""


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    key_hash: bytes  # the entry's key: its answer is sealed to it
    ciphertext: bytes
    wrapped_key: bytes
    key_id: str
    cost: int
    similarity: float | None = None


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(format(v, ".7g") for v in values) + "]"


def _answer(row: RowMapping) -> CachedAnswer:
    similarity = row.get("similarity")
    return CachedAnswer(
        bytes(row["key_hash"]),
        bytes(row["ciphertext"]),
        bytes(row["wrapped_key"]),
        str(row["key_id"]),
        int(row["cost_micro_usd"]),
        float(similarity) if similarity is not None else None,
    )


class CacheRepository:
    async def settings(self, conn: AsyncConnection, *, lock: bool = False) -> CacheSettings:
        row = await fetch_one(conn, _SETTINGS_LOCKED if lock else _SETTINGS)
        raw = row["cache"] if row is not None else None
        return CacheSettings.parse(raw if isinstance(raw, dict) else None)

    async def set_settings(self, conn: AsyncConnection, settings: CacheSettings) -> None:
        await fetch_one(conn, _SET_SETTINGS, {"cache": orjson.dumps(settings.to_dict()).decode()})

    async def exact(self, conn: AsyncConnection, key: bytes) -> CachedAnswer | None:
        row = await fetch_one(conn, _EXACT, {"key": key})
        return _answer(row) if row is not None else None

    async def nearest(
        self, conn: AsyncConnection, partition: bytes, query: Sequence[float], model: str
    ) -> CachedAnswer | None:
        for statement in _TUNE:
            await conn.execute(text(statement))
        params = {"partition": partition, "query": _vector(query), "model": model}
        row = await fetch_one(conn, _NEAREST, params)
        return _answer(row) if row is not None else None

    async def store(
        self,
        conn: AsyncConnection,
        *,
        key: bytes,
        partition: bytes,
        ciphertext: bytes,
        wrapped_key: bytes,
        key_id: str,
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
                "ciphertext": ciphertext,
                "wrapped_key": wrapped_key,
                "key_id": key_id,
                "cost": cost,
                "embedding": _vector(embedding) if embedding is not None else None,
                "embedding_model": embedding_model if embedding is not None else None,
                "ttl": ttl_s,
            },
        )


class CacheJanitorRepository:
    """The worker's housekeeping: it reads ids, orgs and times, never an answer."""

    async def purge_expired(self, conn: AsyncConnection, *, batch: int) -> int:
        result = await conn.execute(text(_PURGE), {"batch": batch})
        return int(result.rowcount)

    async def over_cap(self, conn: AsyncConnection, *, cap: int) -> list[tuple[UUID, int]]:
        rows = await fetch_all(conn, _OVER_CAP, {"cap": cap})
        return [(row["org_id"], int(row["excess"])) for row in rows]

    async def evict_oldest(self, conn: AsyncConnection, *, org_id: UUID, count: int) -> int:
        result = await conn.execute(text(_EVICT), {"org": org_id, "n": count})
        return int(result.rowcount)


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
