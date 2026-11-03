"""Postgres counter store: one atomic upsert per hit on an unlogged table (no extra service)."""

import random
import time

from gateway.adapters.postgres.database import Database, fetch_one
from gateway.ports.counters import RateDecision

_CLEANUP_PROBABILITY = 0.005


class PostgresCounterStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def hit(self, key: str, *, limit: int, window_s: int) -> RateDecision:
        window_start = int(time.time()) // window_s * window_s
        async with self._db.transaction() as conn:
            row = await fetch_one(
                conn,
                "INSERT INTO rate_limit_counters (key, window_start, hits)"
                " VALUES (:k, to_timestamp(:w), 1)"
                " ON CONFLICT (key, window_start) DO UPDATE"
                " SET hits = rate_limit_counters.hits + 1 RETURNING hits",
                {"k": key, "w": window_start},
            )
            # Sampling for housekeeping, not security.
            if random.random() < _CLEANUP_PROBABILITY:  # noqa: S311  # nosec B311
                await conn.exec_driver_sql(
                    "DELETE FROM rate_limit_counters WHERE window_start < now() - interval '1 hour'"
                )
        hits = int(row["hits"]) if row else 1
        retry_after = max(1, window_start + window_s - int(time.time()))
        return RateDecision(
            allowed=hits <= limit, hits=hits, limit=limit, retry_after_s=retry_after
        )
