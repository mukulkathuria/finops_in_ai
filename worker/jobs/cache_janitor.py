"""Response cache housekeeping (US-18; Phase 3 review): expired answers are deleted in batches,
and an org over its entry cap loses its oldest answers first. It runs as the worker role, which
sees ids, orgs and times but cannot read an answer."""

from dataclasses import dataclass

from gateway.adapters.postgres.cache import CacheJanitorRepository
from gateway.adapters.postgres.database import Database


@dataclass(frozen=True, slots=True)
class CacheReport:
    purged: int  # expired
    evicted: int  # over an org's cap


class CacheJanitor:
    def __init__(
        self,
        db: Database,
        *,
        max_entries_per_org: int,
        batch: int = 1_000,
        max_batches: int = 100,
    ) -> None:
        self._db = db
        self._cap = max_entries_per_org
        self._batch = batch
        self._max_batches = max_batches  # per pass: short transactions, the rest waits
        self._repo = CacheJanitorRepository()

    async def run_once(self) -> CacheReport:
        """One pass; purging and eviction each have their own batch budget, so a backlog of
        expired entries never starves the cap. The cap is soft: it holds after each pass."""
        purged = 0
        for _ in range(self._max_batches):
            async with self._db.transaction() as conn:
                deleted = await self._repo.purge_expired(conn, batch=self._batch)
            purged += deleted
            if deleted < self._batch:
                break
        async with self._db.transaction() as conn:
            over = await self._repo.over_cap(conn, cap=self._cap)
        budget = self._max_batches
        evicted = 0
        for org_id, excess in over:
            left = excess
            while left > 0 and budget > 0:
                budget -= 1
                async with self._db.transaction() as conn:
                    deleted = await self._repo.evict_oldest(
                        conn, org_id=org_id, count=min(left, self._batch)
                    )
                if deleted == 0:
                    break
                evicted += deleted
                left -= deleted
        return CacheReport(purged, evicted)
