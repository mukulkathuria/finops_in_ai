"""Budget janitor queries (spec section 6, step 7). They run as the `worker` role, across orgs."""

from datetime import date
from typing import Final
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one

# Holds of requests that outlived their TTL (a crashed process). SKIP LOCKED leaves holds that a
# settlement is closing right now to that settlement.
_EXPIRE: Final = """
WITH picked AS (
  SELECT id FROM budget_reservations
   WHERE status = 'reserved' AND expires_at < now()
   ORDER BY expires_at
   LIMIT :limit
     FOR UPDATE SKIP LOCKED
),
gone AS (
  UPDATE budget_reservations r SET status = 'expired'
    FROM picked WHERE r.id = picked.id
  RETURNING r.budget_id, r.period_start, r.amount
)
SELECT budget_id, period_start, CAST(sum(amount) AS bigint) AS amount, count(*) AS holds
  FROM gone
 GROUP BY budget_id, period_start
 ORDER BY budget_id, period_start
"""

_RETURN_HOLDS: Final = """
UPDATE budget_usage SET reserved = greatest(reserved - :amount, 0)
 WHERE budget_id = :id AND period_start = :p
RETURNING reserved
"""

# The counters of each budget's current period (org timezone): what enforcement reads today.
_CURRENT: Final = """
SELECT bu.budget_id, bu.period_start
  FROM budget_usage bu
  JOIN budgets b ON b.id = bu.budget_id
  JOIN orgs o ON o.id = b.org_id
 WHERE bu.period_start = budget_period_start(b.period, now(), o.timezone)
 ORDER BY bu.budget_id
"""

_PURGE: Final = """
WITH old AS (
  SELECT org_id, caller, key FROM idempotency_keys
   WHERE created_at < now() - make_interval(secs => :age)
   LIMIT :limit
)
DELETE FROM idempotency_keys k USING old
 WHERE k.org_id = old.org_id AND k.caller = old.caller AND k.key = old.key
RETURNING 1
"""


class JanitorRepository:
    async def expire(self, conn: AsyncConnection, *, limit: int) -> list[RowMapping]:
        """Mark up to `limit` overdue holds expired; returns their sums per counter, sorted in
        the global lock order (budget, then period)."""
        return await fetch_all(conn, _EXPIRE, {"limit": limit})

    async def return_holds(
        self, conn: AsyncConnection, budget_id: UUID, period_start: date, amount: int
    ) -> None:
        await fetch_one(conn, _RETURN_HOLDS, {"id": budget_id, "p": period_start, "amount": amount})

    async def current_counters(self, conn: AsyncConnection) -> list[tuple[UUID, date]]:
        rows = await fetch_all(conn, _CURRENT)
        return [(r["budget_id"], r["period_start"]) for r in rows]

    async def purge_idempotency(self, conn: AsyncConnection, *, age_s: float, limit: int) -> int:
        return len(await fetch_all(conn, _PURGE, {"age": age_s, "limit": limit}))
