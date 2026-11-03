"""Budget storage (spec section 6): reserve, then commit; release on failure.

Lock order: every statement that changes `budget_usage` rows takes them in budget-id order (the
reservation upsert sorts its input; settlement updates one row at a time in that order), so
concurrent reservations, settlements and the janitor can never deadlock.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.domain.budgets import (
    Allocation,
    Budget,
    BudgetChange,
    BudgetExceeded,
    BudgetRequest,
    BudgetSettings,
    BudgetStatus,
    Demand,
)
from gateway.domain.errors import Conflict

# The spec's conditional UPDATE as one ordered upsert across every budget that applies to the
# caller: a new period row starts with this hold, an existing row takes it only while
# committed + reserved + hold stays within the limit. The final SELECT lists the budgets that
# refused, tightest scope first; the caller then rolls the whole transaction back.
_RESERVE = """
WITH org AS (
  SELECT timezone FROM orgs WHERE id = app_current_org()
),
cand AS (
  SELECT b.id, b.org_id, b.scope_type, b.team_id, b.user_id, b.period, b.unit,
         budget_period_start(b.period, :at, org.timezone) AS period_start,
         CASE b.unit WHEN 'usd' THEN CAST(:usd AS bigint)
                     WHEN 'tokens' THEN CAST(:tokens AS bigint)
                     ELSE CAST(:images AS bigint) END AS amount
    FROM budgets b CROSS JOIN org
   WHERE b.scope_type = 'org'
      OR (b.scope_type = 'team' AND b.team_id = :team)
      OR (b.scope_type = 'member' AND b.team_id = :team AND b.user_id = :user)
),
held AS (
  INSERT INTO budget_usage AS bu (org_id, budget_id, period_start, reserved)
  SELECT org_id, id, period_start, amount FROM cand WHERE amount > 0 ORDER BY id
  ON CONFLICT (budget_id, period_start) DO UPDATE SET reserved = bu.reserved + excluded.reserved
   WHERE bu.committed + bu.reserved + excluded.reserved
         <= (SELECT l.limit_amount FROM budgets l WHERE l.id = bu.budget_id) + bu.granted
  RETURNING bu.budget_id,
            bu.committed + bu.reserved
              <= (SELECT l.limit_amount FROM budgets l WHERE l.id = bu.budget_id) + bu.granted
              AS fits
),
kept AS (
  INSERT INTO budget_reservations (org_id, request_id, budget_id, period_start, amount, expires_at)
  SELECT c.org_id, :hold, c.id, c.period_start, c.amount, :expires
    FROM cand c JOIN held h ON h.budget_id = c.id AND h.fits
  RETURNING budget_id
)
SELECT c.id, c.scope_type, c.team_id, c.user_id, c.period, c.unit
  FROM cand c LEFT JOIN held h ON h.budget_id = c.id
 WHERE c.amount > 0 AND h.fits IS NOT TRUE
 ORDER BY CASE c.scope_type WHEN 'member' THEN 0 WHEN 'team' THEN 1 ELSE 2 END, c.id
"""

# An expired hold was already returned to the pool by the janitor: only a live one is subtracted.
_CLOSE_HOLDS = """
UPDATE budget_reservations r SET status = :status
  FROM budgets b
 WHERE r.request_id = :hold AND r.status IN ('reserved', 'expired') AND b.id = r.budget_id
RETURNING r.budget_id, r.period_start, b.unit,
          CASE WHEN old.status = 'reserved' THEN r.amount ELSE 0 END AS held
"""

# Swap the hold for the actual amount. The first crossing of the soft threshold in a period
# flips `soft_alerted` and records exactly one alert.
_SETTLE_ROW = """
WITH u AS (
  UPDATE budget_usage bu
     SET reserved = greatest(bu.reserved - :held, 0),
         committed = bu.committed + :actual,
         soft_alerted = bu.soft_alerted OR (
           b.limit_amount + bu.granted > 0
           AND (bu.committed + :actual) * 100 >= (b.limit_amount + bu.granted) * b.soft_pct)
    FROM budgets b
   WHERE bu.budget_id = :id AND bu.period_start = :p AND b.id = bu.budget_id
  RETURNING bu.org_id, bu.budget_id, bu.period_start, new.committed AS committed,
            b.limit_amount + new.granted AS limit_amount,
            new.soft_alerted AND NOT old.soft_alerted AS fired
)
INSERT INTO budget_alerts (org_id, budget_id, period_start, kind, committed, limit_amount)
SELECT org_id, budget_id, period_start, 'soft_threshold', committed, limit_amount FROM u WHERE fired
ON CONFLICT DO NOTHING
RETURNING id
"""

_UPSERT = """
INSERT INTO budgets AS b (org_id, scope_type, team_id, user_id, period, unit, limit_amount,
                          soft_pct, created_by)
VALUES (:org, :scope, :team, :user, :period, :unit, :limit, :soft, :actor)
ON CONFLICT (org_id, scope_type, team_id, user_id, period, unit)
DO UPDATE SET limit_amount = excluded.limit_amount, soft_pct = excluded.soft_pct
RETURNING b.id, b.scope_type, b.team_id, b.user_id, b.period, b.unit, b.limit_amount, b.soft_pct,
          old.limit_amount AS old_limit, old.soft_pct AS old_soft
"""

_GET = """
SELECT id, scope_type, team_id, user_id, period, unit, limit_amount, soft_pct
  FROM budgets WHERE id = :id
"""

_DELETE = """
DELETE FROM budgets WHERE id = :id
RETURNING id, scope_type, team_id, user_id, period, unit, limit_amount, soft_pct
"""

# `:teams`, `:id` and `:user` narrow the list; NULL leaves that filter off.
_STATUSES = """
SELECT b.id, b.scope_type, b.team_id, b.user_id, b.period, b.unit, b.limit_amount, b.soft_pct,
       x.period_start, coalesce(u.committed, 0) AS committed,
       coalesce(u.reserved, 0) AS reserved, coalesce(u.granted, 0) AS granted
  FROM budgets b
  JOIN orgs o ON o.id = b.org_id
  CROSS JOIN LATERAL (SELECT budget_period_start(b.period, :at, o.timezone) AS period_start) x
  LEFT JOIN budget_usage u ON u.budget_id = b.id AND u.period_start = x.period_start
 WHERE (CAST(:teams AS uuid[]) IS NULL OR b.team_id = ANY(CAST(:teams AS uuid[])))
   AND (CAST(:id AS uuid) IS NULL OR b.id = CAST(:id AS uuid))
   AND (CAST(:user AS uuid) IS NULL OR b.user_id = CAST(:user AS uuid))
 ORDER BY CASE b.scope_type WHEN 'org' THEN 0 WHEN 'team' THEN 1 ELSE 2 END,
          b.team_id NULLS FIRST, b.user_id NULLS FIRST, b.period, b.unit
"""

_ALERTS = """
SELECT a.id AS alert_id, a.period_start, a.kind, a.committed AS alert_committed,
       a.limit_amount AS alert_limit, a.created_at,
       b.id, b.scope_type, b.team_id, b.user_id, b.period, b.unit, b.limit_amount, b.soft_pct
  FROM budget_alerts a JOIN budgets b ON b.id = a.budget_id
 WHERE (CAST(:teams AS uuid[]) IS NULL OR b.team_id = ANY(CAST(:teams AS uuid[])))
 ORDER BY a.created_at DESC, a.id DESC
 LIMIT :limit
"""

# The team budget for one period and unit, locked: limit changes inside a team serialize on it.
_TEAM_CAP = """
SELECT limit_amount FROM budgets
 WHERE scope_type = 'team' AND team_id = :team AND period = :period AND unit = :unit
 FOR UPDATE
"""

_MEMBER_TOTAL = """
SELECT coalesce(sum(limit_amount), 0) AS total FROM budgets
 WHERE scope_type = 'member' AND team_id = :team AND period = :period AND unit = :unit
   AND (CAST(:user AS uuid) IS NULL OR user_id <> CAST(:user AS uuid))
"""

# A team whose member limits sum above its cap (strict allocation forbids it).
_OVERALLOCATED = """
SELECT t.team_id FROM budgets t
 WHERE t.scope_type = 'team'
   AND t.limit_amount < (SELECT coalesce(sum(m.limit_amount), 0) FROM budgets m
                          WHERE m.scope_type = 'member' AND m.team_id = t.team_id
                            AND m.period = t.period AND m.unit = t.unit)
 LIMIT 1
"""

_CREATE_REQUEST = """
INSERT INTO budget_requests (org_id, team_id, user_id, budget_id, amount, unit, reason)
VALUES (:org, :team, :user, :budget, :amount, :unit, :reason)
RETURNING id, budget_id, team_id, user_id, amount, unit, reason, status, grant_type,
          decided_by, decided_at, decision_note, created_at
"""

_GET_REQUEST = """
SELECT id, budget_id, team_id, user_id, amount, unit, reason, status, grant_type,
       decided_by, decided_at, decision_note, created_at
  FROM budget_requests WHERE id = :id
"""

_LOCK_REQUEST = """
SELECT id, budget_id, team_id, user_id, amount, unit, reason, status, grant_type,
       decided_by, decided_at, decision_note, created_at
  FROM budget_requests WHERE id = :id
   FOR UPDATE
"""

_LIST_REQUESTS = """
SELECT id, budget_id, team_id, user_id, amount, unit, reason, status, grant_type,
       decided_by, decided_at, decision_note, created_at
  FROM budget_requests
 WHERE (CAST(:everything AS boolean) OR team_id = ANY(CAST(:teams AS uuid[]))
        OR user_id = CAST(:user AS uuid))
   AND (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
 ORDER BY created_at DESC, id DESC
 LIMIT 200
"""

_DECIDE = """
UPDATE budget_requests
   SET status = :status, grant_type = :grant, decided_by = :actor, decided_at = now(),
       decision_note = :note
 WHERE id = :id AND status = 'pending'
RETURNING id, budget_id, team_id, user_id, amount, unit, reason, status, grant_type,
          decided_by, decided_at, decision_note, created_at
"""

# A one-time approval: extra budget for the current period only (the next period starts at 0).
_GRANT_ONCE = """
INSERT INTO budget_usage AS bu (org_id, budget_id, period_start, granted)
SELECT b.org_id, b.id, budget_period_start(b.period, :at, o.timezone), CAST(:amount AS bigint)
  FROM budgets b JOIN orgs o ON o.id = b.org_id
 WHERE b.id = :id
ON CONFLICT (budget_id, period_start) DO UPDATE SET granted = bu.granted + excluded.granted
RETURNING bu.granted
"""

_ENSURE_ROW = """
INSERT INTO budget_usage (org_id, budget_id, period_start)
SELECT b.org_id, b.id, budget_period_start(b.period, :at, o.timezone)
  FROM budgets b JOIN orgs o ON o.id = b.org_id
 WHERE b.id = :id
ON CONFLICT (budget_id, period_start) DO NOTHING
RETURNING period_start
"""

_LOCK_ROW = """
SELECT committed FROM budget_usage WHERE budget_id = :id AND period_start = :p FOR UPDATE
"""

# Sources of truth for one counter: `committed` is every ledger event of the budget's scope
# whose request started inside the period (org timezone); `reserved` is the live holds.
_REBUILD = """
UPDATE budget_usage bu
   SET committed = t.committed, reserved = t.reserved
  FROM (
    SELECT
      (SELECT CAST(coalesce(sum(CASE b.unit WHEN 'usd' THEN e.cost_micro_usd
                                            WHEN 'tokens' THEN e.input_tokens + e.output_tokens
                                            ELSE e.images END), 0) AS bigint)
         FROM usage_events e
        WHERE e.org_id = b.org_id
          AND (b.scope_type = 'org'
               OR (e.team_id = b.team_id AND (b.scope_type = 'team' OR e.user_id = b.user_id)))
          AND e.ts >= CAST(CAST(:p AS date) AS timestamp) AT TIME ZONE o.timezone
          AND e.ts < CAST(budget_period_end(b.period, CAST(:p AS date)) AS timestamp)
                     AT TIME ZONE o.timezone) AS committed,
      (SELECT CAST(coalesce(sum(r.amount), 0) AS bigint) FROM budget_reservations r
        WHERE r.budget_id = b.id AND r.period_start = CAST(:p AS date)
          AND r.status = 'reserved') AS reserved
      FROM budgets b JOIN orgs o ON o.id = b.org_id
     WHERE b.id = :id
  ) t
 WHERE bu.budget_id = :id AND bu.period_start = :p
   AND (bu.committed <> t.committed OR bu.reserved <> t.reserved)
RETURNING old.committed AS was_committed, old.reserved AS was_reserved,
          new.committed AS committed, new.reserved AS reserved
"""

# One team cap at a time: serialises allocation checks even before the team budget row exists.
_LOCK_ALLOCATION = """
SELECT 1 AS locked FROM (SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))) x
"""

_CURRENT_PERIOD = """
SELECT budget_period_start(b.period, :at, o.timezone) AS period_start
  FROM budgets b JOIN orgs o ON o.id = b.org_id
 WHERE b.id = :id
"""

_BUDGET_IDS = "SELECT id FROM budgets ORDER BY id"


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    id: UUID
    budget: Budget
    period_start: date
    kind: str
    committed: int
    limit_amount: int
    created_at: datetime


def _budget(row: RowMapping) -> Budget:
    return Budget(
        id=row["id"],
        scope_type=row["scope_type"],
        team_id=row["team_id"],
        user_id=row["user_id"],
        period=row["period"],
        unit=row["unit"],
        limit_amount=int(row["limit_amount"]),
        soft_pct=int(row["soft_pct"]),
    )


def _scope(row: RowMapping) -> dict[str, Any]:
    return {
        "type": row["scope_type"],
        "budget_id": str(row["id"]),
        "team_id": str(row["team_id"]) if row["team_id"] else None,
        "user_id": str(row["user_id"]) if row["user_id"] else None,
        "period": row["period"],
        "unit": row["unit"],
    }


def _teams(team_ids: Sequence[UUID] | None) -> list[UUID] | None:
    return list(team_ids) if team_ids is not None else None


class BudgetRepository:
    async def reserve(
        self,
        conn: AsyncConnection,
        *,
        hold: str,
        team_id: UUID | None,
        user_id: UUID | None,
        demand: Demand,
        at: datetime,
        expires_at: datetime,
    ) -> None:
        """Hold the worst case against every applicable budget, or raise `BudgetExceeded` naming
        the tightest scope that refused (the caller's transaction then rolls back)."""
        refused = await fetch_all(
            conn,
            _RESERVE,
            {
                "at": at,
                "usd": demand.usd_micro,
                "tokens": demand.tokens,
                "images": demand.images,
                "team": team_id,
                "user": user_id,
                "hold": hold,
                "expires": expires_at,
            },
        )
        if refused:
            first = refused[0]
            raise BudgetExceeded(
                f"{first['scope_type']} budget exhausted for this {first['period']}",
                details={"scope": _scope(first), "request_more_url": "/v1/budget-requests"},
            )

    async def settle(self, conn: AsyncConnection, *, hold: str, actual: Demand | None) -> int:
        """Swap the hold for the actual amount, or release it when `actual` is None.
        Returns how many soft-threshold alerts this settlement fired."""
        rows = await fetch_all(
            conn,
            _CLOSE_HOLDS,
            {"hold": hold, "status": "released" if actual is None else "committed"},
        )
        fired = 0
        for row in sorted(rows, key=lambda r: cast(UUID, r["budget_id"])):  # the lock order
            spent = 0 if actual is None else actual.amount(row["unit"])
            if row["held"] == 0 and spent == 0:
                continue
            alerts = await fetch_all(
                conn,
                _SETTLE_ROW,
                {
                    "held": row["held"],
                    "actual": spent,
                    "id": row["budget_id"],
                    "p": row["period_start"],
                },
            )
            fired += len(alerts)
        return fired

    async def upsert(
        self, conn: AsyncConnection, *, org_id: UUID, change: BudgetChange, actor: UUID
    ) -> tuple[Budget, Budget | None]:
        """Create or change the budget for this scope, period and unit; returns (new, old)."""
        row = await fetch_one(
            conn,
            _UPSERT,
            {
                "org": org_id,
                "scope": change.scope_type,
                "team": change.team_id,
                "user": change.user_id,
                "period": change.period,
                "unit": change.unit,
                "limit": change.limit_amount,
                "soft": change.soft_pct,
                "actor": actor,
            },
        )
        if row is None:  # pragma: no cover - an upsert always returns its row
            raise RuntimeError("budget upsert returned no row")
        new = _budget(row)
        if row["old_limit"] is None:
            return new, None
        old = Budget(
            new.id,
            new.scope_type,
            new.team_id,
            new.user_id,
            new.period,
            new.unit,
            int(row["old_limit"]),
            int(row["old_soft"]),
        )
        return new, old

    async def get(self, conn: AsyncConnection, budget_id: UUID) -> Budget | None:
        row = await fetch_one(conn, _GET, {"id": budget_id})
        return _budget(row) if row else None

    async def delete(self, conn: AsyncConnection, budget_id: UUID) -> Budget | None:
        row = await fetch_one(conn, _DELETE, {"id": budget_id})
        return _budget(row) if row else None

    async def statuses(
        self,
        conn: AsyncConnection,
        *,
        at: datetime,
        team_ids: Sequence[UUID] | None = None,
        budget_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> list[BudgetStatus]:
        """Budgets with their counters for the period that contains `at` (org timezone).
        `team_ids=None` means every budget of the org; otherwise budgets of those teams only."""
        rows = await fetch_all(
            conn, _STATUSES, {"at": at, "teams": _teams(team_ids), "id": budget_id, "user": user_id}
        )
        return [
            BudgetStatus(
                budget=_budget(r),
                period_start=r["period_start"],
                committed=int(r["committed"]),
                reserved=int(r["reserved"]),
                granted=int(r["granted"]),
            )
            for r in rows
        ]

    async def alerts(
        self, conn: AsyncConnection, *, team_ids: Sequence[UUID] | None, limit: int = 200
    ) -> list[BudgetAlert]:
        rows = await fetch_all(conn, _ALERTS, {"teams": _teams(team_ids), "limit": limit})
        return [
            BudgetAlert(
                id=r["alert_id"],
                budget=_budget(r),
                period_start=r["period_start"],
                kind=r["kind"],
                committed=int(r["alert_committed"]),
                limit_amount=int(r["alert_limit"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def settings(self, conn: AsyncConnection) -> BudgetSettings:
        row = await fetch_one(
            conn,
            "SELECT timezone, coalesce(settings ->> 'budget_allocation', 'overcommit')"
            " AS allocation FROM orgs WHERE id = app_current_org()",
        )
        if row is None:  # pragma: no cover - the caller's org always exists
            return BudgetSettings("UTC", "overcommit")
        return BudgetSettings(str(row["timezone"]), cast(Allocation, row["allocation"]))

    async def update_settings(self, conn: AsyncConnection, new: BudgetSettings) -> None:
        await conn.exec_driver_sql(
            "UPDATE orgs SET timezone = %(tz)s,"
            " settings = jsonb_set(settings, '{budget_allocation}', to_jsonb(%(alloc)s::text))"
            " WHERE id = app_current_org()",
            {"tz": new.timezone, "alloc": new.allocation},
        )

    async def timezone_exists(self, conn: AsyncConnection, name: str) -> bool:
        row = await fetch_one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = :n) AS ok",
            {"n": name},
        )
        return bool(row and row["ok"])

    async def team_cap(
        self, conn: AsyncConnection, team_id: UUID, period: str, unit: str
    ) -> int | None:
        """The team budget's limit for this period and unit, locked until the transaction ends."""
        row = await fetch_one(conn, _TEAM_CAP, {"team": team_id, "period": period, "unit": unit})
        return int(row["limit_amount"]) if row else None

    async def member_total(
        self,
        conn: AsyncConnection,
        team_id: UUID,
        period: str,
        unit: str,
        *,
        excluding: UUID | None,
    ) -> int:
        row = await fetch_one(
            conn,
            _MEMBER_TOTAL,
            {"team": team_id, "period": period, "unit": unit, "user": excluding},
        )
        return int(row["total"]) if row else 0

    async def overallocated_team(self, conn: AsyncConnection) -> UUID | None:
        row = await fetch_one(conn, _OVERALLOCATED)
        return cast(UUID, row["team_id"]) if row else None

    async def create_request(
        self,
        conn: AsyncConnection,
        *,
        org_id: UUID,
        budget: Budget,
        user_id: UUID,
        amount: int,
        reason: str,
    ) -> BudgetRequest:
        row = await fetch_one(
            conn,
            _CREATE_REQUEST,
            {
                "org": org_id,
                "team": budget.team_id,
                "user": user_id,
                "budget": budget.id,
                "amount": amount,
                "unit": budget.unit,
                "reason": reason,
            },
        )
        if row is None:  # pragma: no cover - an insert always returns its row
            raise RuntimeError("budget request insert returned no row")
        return BudgetRequest(**dict(row))

    async def get_request(
        self, conn: AsyncConnection, request_id: UUID, *, lock: bool = False
    ) -> BudgetRequest | None:
        row = await fetch_one(conn, _LOCK_REQUEST if lock else _GET_REQUEST, {"id": request_id})
        return BudgetRequest(**dict(row)) if row else None

    async def list_requests(
        self,
        conn: AsyncConnection,
        *,
        everything: bool,
        team_ids: Sequence[UUID],
        user_id: UUID,
        status: str | None,
    ) -> list[BudgetRequest]:
        rows = await fetch_all(
            conn,
            _LIST_REQUESTS,
            {"everything": everything, "teams": list(team_ids), "user": user_id, "status": status},
        )
        return [BudgetRequest(**dict(r)) for r in rows]

    async def decide_request(
        self,
        conn: AsyncConnection,
        request_id: UUID,
        *,
        status: str,
        grant_type: str | None,
        actor: UUID,
        note: str | None,
    ) -> BudgetRequest:
        row = await fetch_one(
            conn,
            _DECIDE,
            {"id": request_id, "status": status, "grant": grant_type, "actor": actor, "note": note},
        )
        if row is None:
            raise Conflict("this budget request was already decided", code="request_decided")
        return BudgetRequest(**dict(row))

    async def grant_once(
        self, conn: AsyncConnection, *, budget_id: UUID, at: datetime, amount: int
    ) -> int:
        row = await fetch_one(conn, _GRANT_ONCE, {"id": budget_id, "at": at, "amount": amount})
        return int(row["granted"]) if row else 0

    async def seed(self, conn: AsyncConnection, budget_id: UUID, at: datetime) -> None:
        """A new budget starts from the spending already made in its current period."""
        await self.rebuild_current(conn, budget_id, at)

    async def rebuild_current(self, conn: AsyncConnection, budget_id: UUID, at: datetime) -> None:
        """Make the counter of the period containing `at` (org timezone) match the ledger and
        the live holds, creating it if needed (a new budget, a changed org timezone)."""
        await fetch_one(conn, _ENSURE_ROW, {"id": budget_id, "at": at})
        row = await fetch_one(conn, _CURRENT_PERIOD, {"id": budget_id, "at": at})
        if row is not None:
            await self.reconcile_row(conn, budget_id, row["period_start"])

    async def lock_allocation(
        self, conn: AsyncConnection, team_id: UUID, period: str, unit: str
    ) -> None:
        """Serialise limit changes for one team cap until the transaction ends."""
        await fetch_one(conn, _LOCK_ALLOCATION, {"key": f"budget-cap:{team_id}:{period}:{unit}"})

    async def budget_ids(self, conn: AsyncConnection) -> list[UUID]:
        return [cast(UUID, r["id"]) for r in await fetch_all(conn, _BUDGET_IDS)]

    async def reconcile_row(
        self, conn: AsyncConnection, budget_id: UUID, period_start: date
    ) -> RowMapping | None:
        """Rebuild one counter from its sources of truth; returns the change, or None when it
        already agreed. The row is locked by an earlier statement, so the ledger read (a new
        snapshot) sees every settlement that committed before the lock was granted, and any later
        settlement adds on top of the rebuilt value."""
        params = {"id": budget_id, "p": period_start}
        if await fetch_one(conn, _LOCK_ROW, params) is None:
            return None
        return await fetch_one(conn, _REBUILD, params)
