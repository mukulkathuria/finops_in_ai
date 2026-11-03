"""Object-level authorization with audited denials (spec section 10: check every object)."""

from typing import NoReturn
from uuid import UUID

from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Forbidden
from gateway.domain.policy import Permission, Principal


class Authorizer:
    def __init__(self, *, db: Database, audit: AuditRepository) -> None:
        self._db = db
        self._audit = audit

    async def ensure(
        self,
        principal: Principal,
        permission: Permission,
        *,
        team_id: UUID | None = None,
        target: str,
        meta: RequestMeta,
    ) -> None:
        if principal.can(permission, team_id):
            return
        await self.deny(principal, [permission], target=target, meta=meta, team_id=team_id)

    async def deny(
        self,
        principal: Principal,
        missing: list[Permission],
        *,
        target: str,
        meta: RequestMeta,
        team_id: UUID | None = None,
        message: str = "you do not have permission for this action",
        code: str | None = None,
    ) -> NoReturn:
        details: dict[str, object] = {"missing_permissions": [p.value for p in missing]}
        if team_id:
            details["team_id"] = str(team_id)
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.ACCESS_DENIED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="object",
                    target_id=target,
                    details=details,
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
        raise Forbidden(message, code=code)
