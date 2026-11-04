"""Routing settings and the routing report (US-19). Org admins choose the two tiers (chat models
of one provider), the complexity threshold, the share of routed requests the other tier answers
too, the tolerance and the teams that opt out; the router itself is switched off, to shadow or on
with the optimization stages (`/v1/admin/stages/router`). Finance reads whether the judged
agreement stays within the tolerance."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.catalog import CatalogCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.routing import (
    RoutingQuality,
    RoutingRepository,
    RoutingSettingsCache,
)
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import InvalidInput
from gateway.domain.policy import Permission, Principal
from gateway.domain.routing import RoutingSettings

MAX_DAYS = 400


@dataclass(frozen=True, slots=True)
class QualityReport:
    start: date
    end: date
    tolerance: float
    quality: RoutingQuality

    @property
    def within_tolerance(self) -> bool | None:
        """None until a sample is judged."""
        mean = self.quality.mean_agreement
        return None if mean is None else mean >= 1.0 - self.tolerance


class RoutingService:
    def __init__(
        self,
        *,
        db: Database,
        repo: RoutingRepository,
        cache: RoutingSettingsCache,
        catalog: CatalogCache,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._cache = cache
        self._catalog = catalog
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def settings(self, principal: Principal, meta: RequestMeta) -> RoutingSettings:
        if not (principal.can(Permission.ORG_MANAGE) or principal.can(Permission.CONFIG_READ)):
            await self._authz.deny(principal, [Permission.CONFIG_READ], target="routing", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._repo.settings(conn)

    async def update(
        self, principal: Principal, changes: dict[str, Any], meta: RequestMeta
    ) -> RoutingSettings:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="routing", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            old = await self._repo.settings(conn, lock=True)
            merged = {**old.to_dict(), **changes}
            merged["opt_out_teams"] = tuple(dict.fromkeys(str(t) for t in merged["opt_out_teams"]))
            try:
                new = RoutingSettings(**merged)
            except ValueError as exc:
                raise InvalidInput(str(exc), code="invalid_routing_settings") from exc
            if new.configured:
                await self._check_tiers(new)
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
                        details={"setting": "routing", "old": old.to_dict(), "new": new.to_dict()},
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
        self._cache.invalidate(principal.org_id)
        return new

    async def _check_tiers(self, settings: RoutingSettings) -> None:
        catalog = await self._catalog.get()
        large = catalog.resolve(settings.large_model or "")
        small = catalog.resolve(settings.small_model or "")
        if large is None or small is None or large.kind != "chat" or small.kind != "chat":
            raise InvalidInput(
                "large_model and small_model must be chat models in the catalog",
                code="model_not_found",
            )
        if large.provider != small.provider:
            raise InvalidInput(
                "the two tiers must be models of one provider", code="invalid_routing_settings"
            )

    async def quality(
        self, principal: Principal, *, start: date, end: date, meta: RequestMeta
    ) -> QualityReport:
        if not principal.can(Permission.REPORTS_READ):
            await self._authz.deny(
                principal, [Permission.REPORTS_READ], target="routing report", meta=meta
            )
        if not 0 < (end - start).days <= MAX_DAYS:
            raise InvalidInput(
                f"end must be after start, at most {MAX_DAYS} days later", param="end"
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            settings = await self._repo.settings(conn)
            quality = await self._repo.quality(
                conn, start=start, end=end, floor=1.0 - settings.tolerance
            )
        return QualityReport(start, end, settings.tolerance, quality)
