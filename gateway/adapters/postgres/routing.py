"""Routing settings and judged samples (US-19). Settings live in the org's settings; each sample
records how far an answer from the small tier agreed with the large tier's answer to the same
request."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final
from uuid import UUID

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_one
from gateway.domain.routing import ROUTER, RoutingSettings

_SETTINGS: Final = "SELECT settings -> 'routing' AS routing FROM orgs WHERE id = app_current_org()"
_SETTINGS_LOCKED: Final = _SETTINGS + " FOR UPDATE"
_SET_SETTINGS: Final = (
    "UPDATE orgs SET settings = jsonb_set(settings, '{routing}', CAST(:routing AS jsonb))"
    " WHERE id = app_current_org() RETURNING id"
)
_SAMPLE: Final = """
INSERT INTO eval_samples (org_id, request_id, stage, candidate_model, reference_model, score)
VALUES (app_current_org(), :request_id, :stage, :candidate, :reference, :score)
"""
_QUALITY: Final = """
WITH w AS (
  SELECT CAST(CAST(:start AS date) AS timestamp) AT TIME ZONE timezone AS lo,
         CAST(CAST(:end AS date) AS timestamp) AT TIME ZONE timezone AS hi
  FROM orgs WHERE id = app_current_org()
),
s AS (
  SELECT count(*) AS samples, avg(score) AS mean,
         count(*) FILTER (WHERE score < :floor) AS below
  FROM eval_samples, w
  WHERE stage = :stage AND created_at >= w.lo AND created_at < w.hi
),
e AS (
  SELECT count(*) FILTER (WHERE route_tier = 'small') AS routed,
         count(*) FILTER (WHERE route_tier = 'large') AS kept
  FROM usage_events, w
  WHERE ts >= w.lo AND ts < w.hi AND route_tier IS NOT NULL
)
SELECT s.samples, s.mean, s.below, e.routed, e.kept FROM s, e
"""


@dataclass(frozen=True, slots=True)
class RoutingQuality:
    samples: int
    mean_agreement: float | None
    below: int  # samples under 1 - tolerance
    routed: int  # requests the small tier answered
    kept: int  # requests for the large tier the router kept there


class RoutingRepository:
    async def settings(self, conn: AsyncConnection, *, lock: bool = False) -> RoutingSettings:
        row = await fetch_one(conn, _SETTINGS_LOCKED if lock else _SETTINGS)
        raw = row["routing"] if row is not None else None
        return RoutingSettings.parse(raw if isinstance(raw, dict) else None)

    async def set_settings(self, conn: AsyncConnection, settings: RoutingSettings) -> None:
        payload = orjson.dumps(settings.to_dict()).decode()
        await fetch_one(conn, _SET_SETTINGS, {"routing": payload})

    async def record_sample(
        self,
        conn: AsyncConnection,
        *,
        request_id: str,
        candidate: str,
        reference: str,
        score: float,
    ) -> None:
        params = {
            "request_id": request_id,
            "stage": ROUTER,
            "candidate": candidate,
            "reference": reference,
            "score": score,
        }
        await conn.execute(text(_SAMPLE), params)

    async def quality(
        self, conn: AsyncConnection, *, start: date, end: date, floor: float
    ) -> RoutingQuality:
        params = {"start": start, "end": end, "floor": floor, "stage": ROUTER}
        row = await fetch_one(conn, _QUALITY, params)
        if row is None:
            return RoutingQuality(0, None, 0, 0, 0)
        mean = row["mean"]
        return RoutingQuality(
            int(row["samples"]),
            float(mean) if mean is not None else None,
            int(row["below"]),
            int(row["routed"]),
            int(row["kept"]),
        )


class RoutingSettingsCache:
    """Each org's routing settings as snapshots (the `PolicyCache` pattern)."""

    def __init__(
        self,
        db: Database,
        repo: RoutingRepository,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, RoutingSettings]] = {}

    async def get(self, org_id: UUID) -> RoutingSettings:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            settings = await self._repo.settings(conn)
        self._entries[org_id] = (self._clock() + self._ttl, settings)
        return settings

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
