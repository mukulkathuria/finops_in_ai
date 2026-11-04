"""Savings settings and the savings report (US-17).

Org admins choose the baseline model (by default each request's own model is its baseline).
Finance reads the report for the org; a team lead with `reports:read_team` reads it for their
team. Totals come from the ledger, so they always match it; the breakdown by technique comes from
the stage reports written with it."""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.catalog import CatalogCache
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.savings import (
    BaselineModels,
    GroupBy,
    LedgerRow,
    SavingsRepository,
    TechniqueRow,
)
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import InvalidInput
from gateway.domain.policy import Permission, Principal

MAX_DAYS = 400


@dataclass(frozen=True, slots=True)
class SavingsReport:
    start: date
    end: date
    team_id: UUID | None
    totals: LedgerRow
    rows: list[LedgerRow]
    techniques: list[TechniqueRow]


class SavingsService:
    def __init__(
        self,
        *,
        db: Database,
        repo: SavingsRepository,
        baselines: BaselineModels,
        catalog: CatalogCache,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._repo = repo
        self._baselines = baselines
        self._catalog = catalog
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def baseline_model(self, principal: Principal, meta: RequestMeta) -> str | None:
        readers = (Permission.ORG_MANAGE, Permission.CONFIG_READ, Permission.REPORTS_READ)
        if not any(principal.can(p) for p in readers):
            await self._authz.deny(principal, [Permission.CONFIG_READ], target="savings", meta=meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._repo.baseline_model(conn)

    async def set_baseline_model(
        self, principal: Principal, model: str | None, meta: RequestMeta
    ) -> str | None:
        await self._authz.ensure(principal, Permission.ORG_MANAGE, target="savings", meta=meta)
        if model is not None:
            entry = (await self._catalog.get()).resolve(model)
            if entry is None or entry.kind != "chat":
                raise InvalidInput(
                    f"'{model}' is not a chat model in the catalog",
                    code="model_not_found",
                    param="baseline_model",
                )
            model = entry.public_name
        async with self._db.transaction(self._ctx(principal)) as conn:
            old = await self._repo.baseline_model(conn, lock=True)
            if old != model:
                await self._repo.set_baseline_model(conn, model)
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.SETTINGS_CHANGED,
                        actor_type="user",
                        actor_id=principal.user_id,
                        target_type="org",
                        target_id=str(principal.org_id),
                        details={"setting": "baseline_model", "old": old, "new": model},
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
        self._baselines.invalidate(principal.org_id)
        return model

    async def report(
        self,
        principal: Principal,
        *,
        start: date,
        end: date,
        group_by: GroupBy | None,
        team_id: UUID | None,
        meta: RequestMeta,
    ) -> SavingsReport:
        org_wide = principal.can(Permission.REPORTS_READ)
        if not org_wide and not (
            team_id is not None and principal.can(Permission.REPORTS_READ_TEAM, team_id)
        ):
            await self._authz.deny(
                principal, [Permission.REPORTS_READ], target="savings report", meta=meta
            )
        if not 0 < (end - start).days <= MAX_DAYS:
            raise InvalidInput(
                f"end must be after start, at most {MAX_DAYS} days later", param="end"
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            (totals,) = await self._repo.ledger(
                conn, start=start, end=end, team=team_id, group_by=None
            )
            rows = (
                await self._repo.ledger(conn, start=start, end=end, team=team_id, group_by=group_by)
                if group_by
                else []
            )
            techniques = await self._repo.techniques(conn, start=start, end=end, team=team_id)
        return SavingsReport(start, end, team_id, totals, rows, techniques)
