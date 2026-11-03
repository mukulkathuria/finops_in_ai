"""The budget janitor (spec section 6, step 7).

- Returns the holds of requests that outlived their TTL (a crashed or killed process).
- Rebuilds each budget's current-period counters from their sources of truth: `committed` from
  the ledger, `reserved` from live holds. Every counter is fixed in its own short transaction
  that locks the row first, so a concurrent settlement lands either before the rebuild (and is
  counted) or after it (and adds on top).
- Purges idempotency keys older than a day.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from gateway.adapters.postgres.budgets import BudgetRepository
from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.janitor import JanitorRepository

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JanitorReport:
    expired: int
    reconciled: int
    purged: int


class BudgetJanitor:
    def __init__(self, db: Database, *, batch: int = 500) -> None:
        self._db = db
        self._batch = batch
        self._budgets = BudgetRepository()
        self._repo = JanitorRepository()

    async def expire_holds(self) -> int:
        expired = 0
        while True:
            async with self._db.transaction() as conn:
                groups = await self._repo.expire(conn, limit=self._batch)
                for g in groups:  # sorted by budget, then period: the global lock order
                    await self._repo.return_holds(
                        conn, g["budget_id"], g["period_start"], int(g["amount"])
                    )
            found = sum(int(g["holds"]) for g in groups)
            expired += found
            if found < self._batch:
                return expired

    async def reconcile(self) -> int:
        """Rebuild the current period's counters; returns how many had drifted."""
        async with self._db.transaction() as conn:
            counters = await self._repo.current_counters(conn)
        drifted = 0
        for budget_id, period_start in counters:
            async with self._db.transaction() as conn:
                fix = await self._budgets.reconcile_row(conn, budget_id, period_start)
            if fix is not None:
                drifted += 1
                log.warning(
                    "budget counter drifted from its sources of truth; rebuilt",
                    extra={
                        "budget_id": str(budget_id),
                        "period_start": period_start.isoformat(),
                        "was_committed": int(fix["was_committed"]),
                        "committed": int(fix["committed"]),
                        "was_reserved": int(fix["was_reserved"]),
                        "reserved": int(fix["reserved"]),
                    },
                )
        return drifted

    async def purge_idempotency(self, *, older_than: timedelta = timedelta(days=1)) -> int:
        purged = 0
        while True:
            async with self._db.transaction() as conn:
                found = await self._repo.purge_idempotency(
                    conn, age_s=older_than.total_seconds(), limit=self._batch
                )
            purged += found
            if found < self._batch:
                return purged

    async def run_once(self, *, reconcile: bool) -> JanitorReport:
        expired = await self.expire_holds()
        if not reconcile:
            return JanitorReport(expired, 0, 0)
        return JanitorReport(expired, await self.reconcile(), await self.purge_idempotency())
