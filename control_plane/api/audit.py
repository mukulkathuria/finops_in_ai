"""Audit trail API (US-28): org admins and auditors read and verify their org's chain."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from control_plane.api.deps import Services
from gateway.adapters.postgres.database import TenantContext
from gateway.api.deps import require
from gateway.domain.policy import Permission, Principal

router = APIRouter(prefix="/v1/admin/audit", tags=["audit"])
AuditReader = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]


class AuditEntryOut(BaseModel):
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


class AuditPageOut(BaseModel):
    data: list[AuditEntryOut]
    next_before_seq: int | None


class VerificationOut(BaseModel):
    ok: bool
    checked: int
    first_bad_seq: int | None


@router.get("")
async def list_audit(
    principal: AuditReader,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
) -> AuditPageOut:
    async with services.db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
        rows = await services.audit.list(conn, limit=limit, before_seq=before_seq)
    entries = [AuditEntryOut.model_validate(r, from_attributes=True) for r in rows]
    next_before = entries[-1].seq if len(entries) == limit else None
    return AuditPageOut(data=entries, next_before_seq=next_before)


@router.get("/verify")
async def verify_audit(principal: AuditReader, services: Services) -> VerificationOut:
    async with services.db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
        result = await services.audit.verify(conn, principal.org_id)
    return VerificationOut(ok=result.ok, checked=result.checked, first_bad_seq=result.first_bad_seq)
