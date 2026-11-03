"""Per-org policy storage and snapshots (spec section 3A: immutable snapshots swapped atomically;
requests read them without locks). A change applies at once in this process and within the TTL
in every other gateway replica."""

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_one
from gateway.domain.guardrails import GuardrailPolicy

_CURRENT = "SELECT settings -> 'guardrails' AS policy FROM orgs WHERE id = app_current_org()"
_LOCKED = (
    "SELECT settings -> 'guardrails' AS policy FROM orgs WHERE id = app_current_org() FOR UPDATE"
)


class PolicyRepository:
    async def guardrails(
        self, conn: AsyncConnection, *, lock: bool = False
    ) -> dict[str, Any] | None:
        row = await fetch_one(conn, _LOCKED if lock else _CURRENT)
        raw = row["policy"] if row else None
        if raw is None:
            return None
        return dict(raw) if isinstance(raw, dict) else dict(orjson.loads(raw))

    async def set_guardrails(self, conn: AsyncConnection, policy: GuardrailPolicy) -> None:
        await fetch_one(
            conn,
            "UPDATE orgs SET settings = jsonb_set(settings, '{guardrails}', CAST(:p AS jsonb))"
            " WHERE id = app_current_org() RETURNING id",
            {"p": orjson.dumps(policy.to_dict()).decode()},
        )


class PolicyCache:
    def __init__(
        self,
        db: Database,
        repo: PolicyRepository,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db = db
        self._repo = repo
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[UUID, tuple[float, GuardrailPolicy]] = {}

    async def get(self, org_id: UUID) -> GuardrailPolicy:
        entry = self._entries.get(org_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        async with self._db.transaction(TenantContext(org_id)) as conn:
            policy = GuardrailPolicy.parse(await self._repo.guardrails(conn), strict=False)
        self._entries[org_id] = (self._clock() + self._ttl, policy)
        return policy

    def invalidate(self, org_id: UUID) -> None:
        self._entries.pop(org_id, None)
