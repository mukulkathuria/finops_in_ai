"""Audit trail persistence (US-28): append in the caller's transaction, list, verify."""

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import orjson
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.domain.audit import AuditEvent, ChainVerification


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    ts: datetime
    actor_id: UUID | None
    actor_type: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip: str | None
    request_id: str | None
    hash: str


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


class AuditRepository:
    async def append(self, conn: AsyncConnection, event: AuditEvent) -> int:
        """Must run inside a tenant transaction; the entry commits with the audited change."""
        row = await fetch_one(
            conn,
            "SELECT audit_append(:actor_id, :actor_type, :action, :target_type, :target_id,"
            " CAST(:details AS jsonb), CAST(:ip AS inet), :request_id) AS seq",
            {
                "actor_id": event.actor_id,
                "actor_type": event.actor_type,
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "details": orjson.dumps(dict(event.details)).decode(),
                "ip": _valid_ip(event.ip),
                "request_id": event.request_id,
            },
        )
        if row is None:  # audit_append always returns the new sequence number
            raise RuntimeError("audit_append returned no row")
        return int(row["seq"])

    async def list(
        self, conn: AsyncConnection, *, limit: int, before_seq: int | None = None
    ) -> list[AuditRecord]:
        rows = await fetch_all(
            conn,
            "SELECT seq, ts, actor_id, actor_type, action, target_type, target_id, details,"
            " host(ip) AS ip, request_id, encode(hash, 'hex') AS hash FROM audit_log"
            " WHERE org_id = app_current_org()"
            " AND (CAST(:before AS bigint) IS NULL OR seq < :before)"
            " ORDER BY seq DESC LIMIT :limit",
            {"before": before_seq, "limit": limit},
        )
        return [AuditRecord(**dict(r)) for r in rows]

    async def verify(self, conn: AsyncConnection, org_id: UUID) -> ChainVerification:
        row = await fetch_one(conn, "SELECT * FROM audit_verify(:org)", {"org": org_id})
        if row is None:  # audit_verify always returns exactly one row
            raise RuntimeError("audit_verify returned no row")
        return ChainVerification(
            ok=bool(row["ok"]), checked=int(row["checked"]), first_bad_seq=row["first_bad_seq"]
        )
