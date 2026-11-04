"""Per-org settings of the relevance filter on retrieved passages (US-21), kept in the org's
settings beside the other sections."""

import time
from collections.abc import Callable
from typing import Final
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_one
from gateway.domain.retrieval import RelevanceSettings

_SETTINGS: Final = (
    "SELECT settings -> 'relevance' AS relevance FROM orgs WHERE id = app_current_org()"
)
_SETTINGS_LOCKED: Final = _SETTINGS + " FOR UPDATE"
_SET_SETTINGS: Final = (
    "UPDATE orgs SET settings = jsonb_set(settings, '{relevance}', CAST(:relevance AS jsonb))"
    " WHERE id = app_current_org() RETURNING id"
)


class RelevanceRepository:
    async def settings(self, conn: AsyncConnection, *, lock: bool = False) -> RelevanceSettings:
        row = await fetch_one(conn, _SETTINGS_LOCKED if lock else _SETTINGS)
        raw = row["relevance"] if row is not None else None
        return RelevanceSettings.parse(raw if isinstance(raw, dict) else None)

    async def set_settings(self, conn: AsyncConnection, settings: RelevanceSettings) -> None:
        payload = orjson.dumps(settings.to_dict()).decode()
        await fetch_one(conn, _SET_SETTINGS, {"relevance": payload})


class RelevanceSettingsCache:
    """Each org's relevance settings as snapshots (the `PolicyCache` pattern)."""

    def __init__(
        self,
        db: Database,
        repo: RelevanceRepository,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, RelevanceSettings]] = {}

    async def get(self, org_id: UUID) -> RelevanceSettings:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            settings = await self._repo.settings(conn)
        self._entries[org_id] = (self._clock() + self._ttl, settings)
        return settings

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
