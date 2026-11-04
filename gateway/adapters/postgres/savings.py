"""Savings settings and reports (US-17). The baseline model lives in the org's settings; the
report reads the ledger (totals by team, model or day) and the stage reports (by technique) for a
range of days in the org's timezone."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one

GroupBy = Literal["team", "model", "day"]

_BASELINE: Final = (
    "SELECT settings -> 'savings' ->> 'baseline_model' AS model FROM orgs"
    " WHERE id = app_current_org()"
)
_BASELINE_LOCKED: Final = _BASELINE + " FOR UPDATE"
_SET_BASELINE: Final = """
UPDATE orgs SET settings = jsonb_set(
  settings, '{savings}',
  coalesce(settings -> 'savings', '{}'::jsonb)
    || jsonb_build_object('baseline_model', CAST(:model AS text)))
WHERE id = app_current_org()
RETURNING id
"""
_TOTALS: Final = """
WITH o AS (SELECT timezone AS tz FROM orgs WHERE id = app_current_org())
SELECT 'all' AS key, NULL AS label, count(*) AS requests,
       coalesce(sum(e.input_tokens), 0) AS input_tokens,
       coalesce(sum(e.output_tokens), 0) AS output_tokens,
       coalesce(sum(e.cost_micro_usd), 0) AS cost,
       coalesce(sum(e.baseline_micro_usd), 0) AS baseline,
       coalesce(sum(e.overhead_micro_usd), 0) AS overhead
FROM usage_events e CROSS JOIN o
WHERE e.ts >= CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE o.tz
  AND e.ts < CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE o.tz
  AND (CAST(:team AS uuid) IS NULL OR e.team_id = CAST(:team AS uuid))
"""
_BY_TEAM: Final = """
WITH o AS (SELECT timezone AS tz FROM orgs WHERE id = app_current_org())
SELECT coalesce(CAST(e.team_id AS text), 'none') AS key, max(t.name) AS label,
       count(*) AS requests, coalesce(sum(e.input_tokens), 0) AS input_tokens,
       coalesce(sum(e.output_tokens), 0) AS output_tokens,
       coalesce(sum(e.cost_micro_usd), 0) AS cost,
       coalesce(sum(e.baseline_micro_usd), 0) AS baseline,
       coalesce(sum(e.overhead_micro_usd), 0) AS overhead
FROM usage_events e CROSS JOIN o LEFT JOIN teams t ON t.id = e.team_id
WHERE e.ts >= CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE o.tz
  AND e.ts < CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE o.tz
  AND (CAST(:team AS uuid) IS NULL OR e.team_id = CAST(:team AS uuid))
GROUP BY e.team_id
ORDER BY baseline DESC, key
"""
_BY_MODEL: Final = """
WITH o AS (SELECT timezone AS tz FROM orgs WHERE id = app_current_org())
SELECT e.provider || '/' || e.model AS key, NULL AS label, count(*) AS requests,
       coalesce(sum(e.input_tokens), 0) AS input_tokens,
       coalesce(sum(e.output_tokens), 0) AS output_tokens,
       coalesce(sum(e.cost_micro_usd), 0) AS cost,
       coalesce(sum(e.baseline_micro_usd), 0) AS baseline,
       coalesce(sum(e.overhead_micro_usd), 0) AS overhead
FROM usage_events e CROSS JOIN o
WHERE e.ts >= CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE o.tz
  AND e.ts < CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE o.tz
  AND (CAST(:team AS uuid) IS NULL OR e.team_id = CAST(:team AS uuid))
GROUP BY e.provider, e.model
ORDER BY baseline DESC, key
"""
_BY_DAY: Final = """
WITH o AS (SELECT timezone AS tz FROM orgs WHERE id = app_current_org())
SELECT to_char(e.ts AT TIME ZONE o.tz, 'YYYY-MM-DD') AS key, NULL AS label,
       count(*) AS requests, coalesce(sum(e.input_tokens), 0) AS input_tokens,
       coalesce(sum(e.output_tokens), 0) AS output_tokens,
       coalesce(sum(e.cost_micro_usd), 0) AS cost,
       coalesce(sum(e.baseline_micro_usd), 0) AS baseline,
       coalesce(sum(e.overhead_micro_usd), 0) AS overhead
FROM usage_events e CROSS JOIN o
WHERE e.ts >= CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE o.tz
  AND e.ts < CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE o.tz
  AND (CAST(:team AS uuid) IS NULL OR e.team_id = CAST(:team AS uuid))
GROUP BY 1
ORDER BY 1
"""
_TECHNIQUES: Final = """
WITH o AS (SELECT timezone AS tz FROM orgs WHERE id = app_current_org())
SELECT s.stage,
       count(*) FILTER (WHERE s.applied AND NOT s.shadow) AS applied,
       count(*) FILTER (WHERE s.shadow) AS shadow_runs,
       coalesce(sum(s.tokens_before - s.tokens_after) FILTER (WHERE s.applied AND NOT s.shadow), 0)
         AS tokens_saved,
       coalesce(sum(-s.cost_delta_micro_usd) FILTER (WHERE s.applied AND NOT s.shadow), 0)
         AS saved,
       coalesce(sum(-s.cost_delta_micro_usd) FILTER (WHERE s.applied AND s.shadow), 0)
         AS shadow_saved
FROM stage_reports s CROSS JOIN o
WHERE s.ts >= CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE o.tz
  AND s.ts < CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE o.tz
  AND (CAST(:team AS uuid) IS NULL OR EXISTS (
        SELECT 1 FROM usage_events e
         WHERE e.org_id = s.org_id AND e.request_id = s.request_id AND e.ts = s.ts
           AND e.team_id = CAST(:team AS uuid)))
GROUP BY s.stage
ORDER BY saved DESC, s.stage
"""
_GROUPED: Final[dict[str, str]] = {"team": _BY_TEAM, "model": _BY_MODEL, "day": _BY_DAY}


@dataclass(frozen=True, slots=True)
class LedgerRow:
    key: str
    label: str | None
    requests: int
    input_tokens: int
    output_tokens: int
    cost: int
    baseline: int
    overhead: int

    @property
    def savings(self) -> int:
        return self.baseline - self.cost - self.overhead


@dataclass(frozen=True, slots=True)
class TechniqueRow:
    stage: str
    applied: int
    shadow_runs: int
    tokens_saved: int
    saved: int
    shadow_saved: int  # what the technique would have saved where it ran in shadow mode


class SavingsRepository:
    async def baseline_model(self, conn: AsyncConnection, *, lock: bool = False) -> str | None:
        row = await fetch_one(conn, _BASELINE_LOCKED if lock else _BASELINE)
        return str(row["model"]) if row is not None and row["model"] else None

    async def set_baseline_model(self, conn: AsyncConnection, model: str | None) -> None:
        await fetch_one(conn, _SET_BASELINE, {"model": model})

    async def ledger(
        self,
        conn: AsyncConnection,
        *,
        start: date,
        end: date,
        team: UUID | None,
        group_by: GroupBy | None,
    ) -> list[LedgerRow]:
        sql = _GROUPED[group_by] if group_by else _TOTALS
        rows = await fetch_all(conn, sql, {"start": start, "end": end, "team": team})
        return [LedgerRow(**dict(r)) for r in rows]

    async def techniques(
        self, conn: AsyncConnection, *, start: date, end: date, team: UUID | None
    ) -> list[TechniqueRow]:
        rows = await fetch_all(conn, _TECHNIQUES, {"start": start, "end": end, "team": team})
        return [TechniqueRow(**dict(r)) for r in rows]


class BaselineModels:
    """Each org's baseline model as a snapshot (the `PolicyCache` pattern)."""

    def __init__(
        self,
        db: Database,
        repo: SavingsRepository,
        *,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, str | None]] = {}

    async def get(self, org_id: UUID) -> str | None:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            model = await self._repo.baseline_model(conn)
        self._entries[org_id] = (self._clock() + self._ttl, model)
        return model

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
