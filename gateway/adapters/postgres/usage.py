"""The usage ledger: one append-only row per request (spec section 5)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncConnection

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


class UsageRepository:
    async def record(self, conn: AsyncConnection, e: UsageEvent) -> None:
        await conn.exec_driver_sql(
            "INSERT INTO usage_events (org_id, team_id, user_id, api_key_id, request_id, kind,"
            " provider, model, adapter, catalog_version, input_tokens, output_tokens,"
            " cached_tokens,"
            " cache_write_tokens, reasoning_tokens, images, cost_micro_usd, baseline_micro_usd,"
            " overhead_micro_usd, cache_hit, route_tier, status, estimated, latency_ms, flags, ts)"
            " VALUES (%(org)s, %(team)s, %(user)s, %(key)s, %(rid)s, %(kind)s, %(provider)s,"
            " %(model)s,"
            " %(adapter)s, %(cv)s, %(inp)s, %(out)s, %(cached)s, %(cw)s, %(reason)s, %(images)s,"
            " %(cost)s, %(base)s, %(over)s, %(hit)s, %(tier)s, %(status)s, %(est)s, %(lat)s,"
            " %(flags)s::jsonb, COALESCE(%(ts)s, now()))",
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
                "flags": orjson.dumps(e.flags).decode(),
                "ts": e.ts,
            },
        )
