"""The usage ledger: one append-only row per request (spec section 5)."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all
from gateway.domain.llm import Usage


@dataclass(frozen=True, slots=True)
class UsageEvent:
    org_id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None
    request_id: str
    kind: str
    provider: str
    model: str
    adapter: str
    catalog_version: int
    usage: Usage
    cost_micro_usd: int
    status: str
    latency_ms: int
    images: int = 0
    baseline_micro_usd: int = 0
    overhead_micro_usd: int = 0
    cache_hit: str = "none"
    route_tier: str | None = None
    flags: dict[str, Any] = field(default_factory=dict)
    ts: datetime | None = None  # the request clock; the database time when absent
    hold: str | None = None  # the budget hold: makes the insert idempotent when retried


# One user's ledger totals for the day, week and month that contain `:at`, in the org timezone.
_PERIODS = """
WITH org AS (SELECT timezone FROM orgs WHERE id = app_current_org()),
p AS (
  SELECT x.period, x.ord, org.timezone AS tz,
         budget_period_start(x.period, :at, org.timezone) AS period_start
    FROM org CROSS JOIN (VALUES ('day', 0), ('week', 1), ('month', 2)) AS x (period, ord)
)
SELECT p.period, p.period_start, budget_period_end(p.period, p.period_start) AS period_end,
       count(e.id) AS requests,
       CAST(coalesce(sum(e.cost_micro_usd), 0) AS bigint) AS cost_micro_usd,
       CAST(coalesce(sum(e.input_tokens), 0) AS bigint) AS input_tokens,
       CAST(coalesce(sum(e.output_tokens), 0) AS bigint) AS output_tokens,
       CAST(coalesce(sum(e.images), 0) AS bigint) AS images
  FROM p
  LEFT JOIN usage_events e
    ON e.user_id = :user
   AND e.ts >= (CAST(p.period_start AS timestamp) AT TIME ZONE p.tz)
   AND e.ts < (CAST(budget_period_end(p.period, p.period_start) AS timestamp) AT TIME ZONE p.tz)
 GROUP BY p.period, p.ord, p.period_start
 ORDER BY p.ord
"""

# Typed casts throughout: an INSERT ... SELECT cannot infer parameter types from the target.
_RECORD = """
INSERT INTO usage_events (org_id, team_id, user_id, api_key_id, request_id, kind, provider, model,
  adapter, catalog_version, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
  reasoning_tokens, images, cost_micro_usd, baseline_micro_usd, overhead_micro_usd, cache_hit,
  route_tier, status, estimated, latency_ms, flags, ts)
SELECT CAST(%(org)s AS uuid), CAST(%(team)s AS uuid), CAST(%(user)s AS uuid),
  CAST(%(key)s AS uuid), CAST(%(rid)s AS text), CAST(%(kind)s AS text),
  CAST(%(provider)s AS text), CAST(%(model)s AS text), CAST(%(adapter)s AS text),
  CAST(%(cv)s AS bigint), CAST(%(inp)s AS bigint), CAST(%(out)s AS bigint),
  CAST(%(cached)s AS bigint), CAST(%(cw)s AS bigint), CAST(%(reason)s AS bigint),
  CAST(%(images)s AS bigint), CAST(%(cost)s AS bigint), CAST(%(base)s AS bigint),
  CAST(%(over)s AS bigint), CAST(%(hit)s AS text), CAST(%(tier)s AS text),
  CAST(%(status)s AS text), CAST(%(est)s AS boolean), CAST(%(lat)s AS integer),
  CAST(%(flags)s AS jsonb), COALESCE(CAST(%(ts)s AS timestamptz), now())
WHERE CAST(%(hold)s AS text) IS NULL OR NOT EXISTS (
  SELECT 1 FROM usage_events
   WHERE org_id = CAST(%(org)s AS uuid) AND request_id = CAST(%(rid)s AS text)
     AND ts = CAST(%(ts)s AS timestamptz) AND flags ->> 'hold' = CAST(%(hold)s AS text))
"""


@dataclass(frozen=True, slots=True)
class PeriodUsage:
    """One user's ledger totals for one budget period (org timezone)."""

    period: str
    period_start: date
    period_end: date
    requests: int
    cost_micro_usd: int
    input_tokens: int
    output_tokens: int
    images: int


class UsageRepository:
    async def record(self, conn: AsyncConnection, e: UsageEvent) -> None:
        """Append one ledger row. With a hold the insert is idempotent: a settlement retried
        after an unacknowledged commit finds its own row and writes nothing (one charge)."""
        flags = {**e.flags, "hold": e.hold} if e.hold else e.flags
        await conn.exec_driver_sql(
            _RECORD,
            {
                "org": e.org_id,
                "team": e.team_id,
                "user": e.user_id,
                "key": e.api_key_id,
                "rid": e.request_id,
                "kind": e.kind,
                "provider": e.provider,
                "model": e.model,
                "adapter": e.adapter,
                "cv": e.catalog_version,
                "inp": e.usage.input_tokens,
                "out": e.usage.output_tokens,
                "cached": e.usage.cached_tokens,
                "cw": e.usage.cache_write_tokens,
                "reason": e.usage.reasoning_tokens,
                "images": e.images,
                "cost": e.cost_micro_usd,
                "base": e.baseline_micro_usd,
                "over": e.overhead_micro_usd,
                "hit": e.cache_hit,
                "tier": e.route_tier,
                "status": e.status,
                "est": e.usage.estimated,
                "lat": e.latency_ms,
                "flags": orjson.dumps(flags).decode(),
                "ts": e.ts,
                "hold": e.hold,
            },
        )

    async def periods(
        self, conn: AsyncConnection, *, user_id: UUID, at: datetime
    ) -> list[PeriodUsage]:
        rows = await fetch_all(conn, _PERIODS, {"user": user_id, "at": at})
        return [PeriodUsage(**dict(r)) for r in rows]
