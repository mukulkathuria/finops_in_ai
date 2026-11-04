"""Response cache settings (US-18): off, exact or semantic, the time an answer is kept and the
similarity a semantic match needs. Org admins change them; every change is audited."""

from typing import Any

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.cache import CacheRepository, CacheSettingsCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.cache import CacheSettings
from gateway.domain.errors import InvalidInput
from gateway.domain.policy import Permission, Principal


class CacheSettingsService:
    def __init__(
        self,
        *,
        db: Database,
        repo: CacheRepository,
        cache: CacheSettingsCache,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._cache = cache
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def get(self, principal: Principal, meta: RequestMeta) -> CacheSettings:
        if not (principal.can(Permission.ORG_MANAGE) or principal.can(Permission.CONFIG_READ)):
            await self._authz.deny(principal, [Permission.CONFIG_READ], target="cache", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._repo.settings(conn)

    async def update(
        self, principal: Principal, changes: dict[str, Any], meta: RequestMeta
    ) -> CacheSettings:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="cache", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            old = await self._repo.settings(conn, lock=True)
            try:
                new = CacheSettings(**{**old.to_dict(), **changes})
            except ValueError as exc:
                raise InvalidInput(str(exc), code="invalid_cache_settings") from exc
            if new != old:
                await self._repo.set_settings(conn, new)
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.SETTINGS_CHANGED,
                        actor_type="user",
                        actor_id=principal.user_id,
                        target_type="org",
                        target_id=str(principal.org_id),
                        details={"setting": "cache", "old": old.to_dict(), "new": new.to_dict()},
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
        self._cache.invalidate(principal.org_id)
        return new
