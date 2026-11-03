"""Budget administration (US-07, US-08): who may set which limit, with an audit trail.

Org admins set org and team budgets. Every limit change is audited with who, what, the old and
new values and the reason (spec section 6).
"""

from datetime import UTC, datetime
from uuid import UUID

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.budgets import BudgetAlert, BudgetRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.teams import TeamRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.budgets import (
    Budget,
    BudgetChange,
    BudgetSettings,
    BudgetStatus,
)
from gateway.domain.errors import InvalidInput, NotFound
from gateway.domain.policy import Permission, Principal

_MANAGE = {
    "org": Permission.BUDGETS_MANAGE_ORG,
    "team": Permission.BUDGETS_MANAGE_TEAM,
    "member": Permission.BUDGETS_MANAGE_MEMBER,
}


def _describe(budget: Budget) -> dict[str, object]:
    return {
        "scope_type": budget.scope_type,
        "team_id": str(budget.team_id) if budget.team_id else None,
        "user_id": str(budget.user_id) if budget.user_id else None,
        "period": budget.period,
        "unit": budget.unit,
    }


class BudgetService:
    def __init__(
        self,
        *,
        db: Database,
        budgets: BudgetRepository,
        teams: TeamRepository,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._budgets = budgets
        self._teams = teams
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def set_budget(
        self, principal: Principal, change: BudgetChange, meta: RequestMeta
    ) -> tuple[BudgetStatus, bool]:
        """Create or change a limit; returns the budget with its counters and whether it is new."""
        if change.scope_type == "member":
            raise InvalidInput(
                "member budgets are not supported yet", code="invalid_scope", param="scope_type"
            )
        await self._authz.ensure(
            principal,
            _MANAGE[change.scope_type],
            team_id=change.team_id,
            target=f"budgets:{change.scope_type}",
            meta=meta,
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            if change.team_id is not None and not await self._teams.team_exists(
                conn, change.team_id
            ):
                raise NotFound("team not found", code="team_not_found", param="team_id")
            budget, old = await self._budgets.upsert(
                conn, org_id=principal.org_id, change=change, actor=principal.user_id
            )
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.BUDGET_SET,
                    principal,
                    budget,
                    meta,
                    {
                        **_describe(budget),
                        "old_limit": old.limit_amount if old else None,
                        "new_limit": budget.limit_amount,
                        "old_soft_pct": old.soft_pct if old else None,
                        "new_soft_pct": budget.soft_pct,
                        "reason": change.reason,
                    },
                ),
            )
            (status,) = await self._budgets.statuses(
                conn, at=datetime.now(UTC), budget_id=budget.id
            )
        return status, old is None

    async def delete_budget(self, principal: Principal, budget_id: UUID, meta: RequestMeta) -> None:
        async with self._db.transaction(self._ctx(principal)) as conn:
            budget = await self._budgets.get(conn, budget_id)
        if budget is None:
            raise NotFound("budget not found", code="budget_not_found")
        await self._authz.ensure(
            principal,
            _MANAGE[budget.scope_type],
            team_id=budget.team_id,
            target=f"budget:{budget_id}",
            meta=meta,
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            deleted = await self._budgets.delete(conn, budget_id)
            if deleted is None:
                raise NotFound("budget not found", code="budget_not_found")
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.BUDGET_DELETED,
                    principal,
                    deleted,
                    meta,
                    {**_describe(deleted), "old_limit": deleted.limit_amount},
                ),
            )

    async def _visible_teams(
        self, principal: Principal, team_id: UUID | None, meta: RequestMeta
    ) -> list[UUID] | None:
        """None means every budget in the org (org admins and finance); team leads see the
        budgets of the teams they lead; anyone else is denied."""
        if principal.can(Permission.BUDGETS_MANAGE_ORG) or principal.can(Permission.REPORTS_READ):
            return None if team_id is None else [team_id]
        led = sorted(
            t for t in principal.led_team_ids() if principal.can(Permission.REPORTS_READ_TEAM, t)
        )
        if team_id is not None and team_id not in led:
            await self._authz.deny(
                principal,
                [Permission.REPORTS_READ_TEAM],
                target=f"team:{team_id}",
                meta=meta,
                team_id=team_id,
            )
        if team_id is None and not led:
            await self._authz.deny(
                principal, [Permission.REPORTS_READ_TEAM], target="budgets", meta=meta
            )
        return [team_id] if team_id is not None else led

    async def list_budgets(
        self, principal: Principal, team_id: UUID | None, meta: RequestMeta
    ) -> list[BudgetStatus]:
        teams = await self._visible_teams(principal, team_id, meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._budgets.statuses(conn, at=datetime.now(UTC), team_ids=teams)

    async def list_alerts(
        self, principal: Principal, team_id: UUID | None, meta: RequestMeta
    ) -> list[BudgetAlert]:
        teams = await self._visible_teams(principal, team_id, meta)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._budgets.alerts(conn, team_ids=teams)

    async def settings(self, principal: Principal) -> BudgetSettings:
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._budgets.settings(conn)

    async def update_settings(
        self, principal: Principal, *, timezone: str | None, meta: RequestMeta
    ) -> BudgetSettings:
        await self._authz.ensure(
            principal, Permission.BUDGETS_MANAGE_ORG, target="budget_settings", meta=meta
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            old = await self._budgets.settings(conn)
            if timezone is not None and not await self._budgets.timezone_exists(conn, timezone):
                raise InvalidInput(
                    "unknown IANA timezone", code="invalid_timezone", param="timezone"
                )
            new = BudgetSettings(timezone or old.timezone, old.allocation)
            if new != old:
                await self._budgets.update_settings(conn, new)
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.SETTINGS_CHANGED,
                        actor_type="user",
                        actor_id=principal.user_id,
                        target_type="org",
                        target_id=str(principal.org_id),
                        details={
                            "setting": "budgets",
                            "old": {"timezone": old.timezone, "allocation": old.allocation},
                            "new": {"timezone": new.timezone, "allocation": new.allocation},
                        },
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
        return new

    @staticmethod
    def _event(
        action: str,
        principal: Principal,
        budget: Budget,
        meta: RequestMeta,
        details: dict[str, object],
    ) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_type="user",
            actor_id=principal.user_id,
            target_type="budget",
            target_id=str(budget.id),
            details=details,
            ip=meta.ip,
            request_id=meta.request_id,
        )
