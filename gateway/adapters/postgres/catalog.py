"""Price catalog reads, the TTL cache of the immutable Catalog snapshot, and the price sync's
writes: rows are only ever appended."""

import asyncio
import time
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, fetch_all, fetch_one
from gateway.domain.price_list import PriceEntry
from gateway.domain.pricing import Catalog, ModelPrice

_CURRENT: Final = (
    "SELECT DISTINCT ON (provider, model) id AS catalog_version, provider, model, kind,"
    " price_input, price_output, price_cached_input, price_cache_write, price_image,"
    " tier_threshold_tokens, tier_price_input, tier_price_output, context_window,"
    " max_output_tokens FROM model_catalog WHERE effective_from <= now()"
    " ORDER BY provider, model, effective_from DESC"
)
_LOCK: Final = "SELECT pg_try_advisory_xact_lock(hashtext('finops.price_sync')) AS locked"
_INSERT: Final = """
INSERT INTO model_catalog (provider, model, kind, price_input, price_output, price_cached_input,
  price_cache_write, price_image, tier_threshold_tokens, tier_price_input, tier_price_output,
  context_window, max_output_tokens, source, effective_from)
VALUES (:provider, :model, :kind, :price_input, :price_output, :price_cached_input,
  :price_cache_write, :price_image, :tier_threshold_tokens, :tier_price_input,
  :tier_price_output, :context_window, :max_output_tokens, :source,
  coalesce(CAST(:effective_from AS timestamptz), now()))
ON CONFLICT (provider, model, effective_from) DO NOTHING
RETURNING id
"""
_FIELDS: Final = (
    "provider",
    "model",
    "kind",
    "price_input",
    "price_output",
    "price_cached_input",
    "price_cache_write",
    "price_image",
    "tier_threshold_tokens",
    "tier_price_input",
    "tier_price_output",
    "context_window",
    "max_output_tokens",
)


class CatalogRepository:
    async def current(self, conn: AsyncConnection) -> list[ModelPrice]:
        rows = await fetch_all(conn, _CURRENT)
        return [ModelPrice(**dict(r)) for r in rows]

    async def lock_sync(self, conn: AsyncConnection) -> bool:
        """One price sync at a time across workers, for this transaction: False while another
        worker holds it."""
        row = await fetch_one(conn, _LOCK)
        return bool(row is not None and row["locked"])

    async def insert(self, conn: AsyncConnection, entry: PriceEntry, *, source: str) -> bool:
        """Append a row; False when the model already has one from that very time."""
        params: dict[str, object] = {name: getattr(entry.price, name) for name in _FIELDS}
        params |= {"source": source, "effective_from": entry.effective_from}
        return await fetch_one(conn, _INSERT, params) is not None


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
