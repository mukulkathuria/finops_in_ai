"""Own usage and remaining budget (US-09): totals straight from the ledger, counters from the
budget engine, read in one transaction. Only the caller's own rows are ever selected."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from gateway.adapters.postgres.budgets import BudgetRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.usage import PeriodUsage, UsageRepository
from gateway.domain.budgets import BudgetStatus


@dataclass(frozen=True, slots=True)
class UsageReport:
    timezone: str
    periods: list[PeriodUsage]
    budgets: list[BudgetStatus]  # the caller's own member budgets


class UsageReporter:
    def __init__(self, *, db: Database, usage: UsageRepository, budgets: BudgetRepository) -> None:
        self._db = db
        self._usage = usage
        self._budgets = budgets

    async def for_user(self, org_id: UUID, user_id: UUID) -> UsageReport:
        at = datetime.now(UTC)
        async with self._db.transaction(TenantContext(org_id, user_id)) as conn:
            settings = await self._budgets.settings(conn)
            periods = await self._usage.periods(conn, user_id=user_id, at=at)
            budgets = await self._budgets.statuses(conn, at=at, user_id=user_id)
        return UsageReport(settings.timezone, periods, budgets)
