"""Price catalog reads and a TTL cache of the immutable Catalog snapshot."""

import asyncio
import time

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, fetch_all
from gateway.domain.pricing import Catalog, ModelPrice


class CatalogRepository:
    async def current(self, conn: AsyncConnection) -> list[ModelPrice]:
        rows = await fetch_all(
            conn,
            "SELECT DISTINCT ON (provider, model) id AS catalog_version, provider, model, kind,"
            " price_input, price_output, price_cached_input, price_cache_write, price_image,"
            " tier_threshold_tokens, tier_price_input, tier_price_output, context_window,"
            " max_output_tokens FROM model_catalog WHERE effective_from <= now()"
            " ORDER BY provider, model, effective_from DESC",
        )
        return [ModelPrice(**dict(r)) for r in rows]


class CatalogCache:
    def __init__(self, db: Database, repo: CatalogRepository, ttl_seconds: float = 60.0) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_seconds
        self._snapshot: Catalog | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> Catalog:
        if self._snapshot is not None and time.monotonic() - self._loaded_at < self._ttl:
            return self._snapshot
        async with self._lock:  # one refresh at a time; others reuse its result
            if self._snapshot is None or time.monotonic() - self._loaded_at >= self._ttl:
                async with self._db.transaction() as conn:
                    self._snapshot = Catalog(await self._repo.current(conn))
                self._loaded_at = time.monotonic()
        return self._snapshot
