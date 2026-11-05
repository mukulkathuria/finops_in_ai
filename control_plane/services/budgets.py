"""Budget administration (US-07, US-08): who may set which limit, under which cap.

Org admins set org and team budgets. Team leads set member limits inside the teams they lead,
never above the team cap and never their own; under strict allocation the member limits of a
team never sum above its cap, for anyone. Members ask for more budget; the lead (an org admin
for team budgets) approves it once (until period end) or permanently, or denies it. Every change
is audited with who, what, the old and new values and the reason (spec section 6).
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.budgets import BudgetAlert, BudgetRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.teams import TeamRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.budgets import (
    Allocation,
    Budget,
    BudgetChange,
    BudgetRequest,
    BudgetSettings,
    BudgetStatus,
    GrantType,
    check_member_limit,
    check_team_cap,
)
from gateway.domain.errors import Conflict, InvalidInput, NotFound
from gateway.domain.policy import Permission, Principal

_MANAGE = {
    "org": Permission.BUDGETS_MANAGE_ORG,
    "team": Permission.BUDGETS_MANAGE_TEAM,
    "member": Permission.BUDGETS_MANAGE_MEMBER,
}
_OWN_LIMIT = "team leads cannot change their own limit"


def _describe(budget: Budget) -> dict[str, object]:
    return {
        "scope_type": budget.scope_type,
        "team_id": str(budget.team_id) if budget.team_id else None,
        "user_id": str(budget.user_id) if budget.user_id else None,
        "period": budget.period,
        "unit": budget.unit,
    }


def _applies_to(budget: Budget, principal: Principal) -> bool:
    if budget.team_id is None or budget.team_id not in principal.team_ids:
        return False
    return budget.scope_type == "team" or budget.user_id == principal.user_id


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

    @staticmethod
    def _is_admin(principal: Principal, team_id: UUID | None) -> bool:
        """Team-budget rights are org-wide grants: org admins hold them, team leads never do."""
        return principal.can(Permission.BUDGETS_MANAGE_TEAM, team_id)

    async def set_budget(
        self, principal: Principal, change: BudgetChange, meta: RequestMeta
    ) -> tuple[BudgetStatus, bool]:
        """Create or change a limit; returns the budget with its counters and whether it is new."""
        await self._authz.ensure(
            principal,
            _MANAGE[change.scope_type],
            team_id=change.team_id,
            target=f"budgets:{change.scope_type}",
            meta=meta,
        )
        admin = self._is_admin(principal, change.team_id)
        if change.user_id == principal.user_id and not admin:
            await self._authz.deny(
                principal,
                [Permission.BUDGETS_MANAGE_TEAM],
                target=f"budgets:member:{principal.user_id}",
                meta=meta,
                team_id=change.team_id,
                message=_OWN_LIMIT,
                code="own_limit",
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            await self._check_target(conn, change)
            await self._check_allocation(conn, change, capped=not admin)
            return await self._write(conn, principal, change, meta)

    async def _check_target(self, conn: AsyncConnection, change: BudgetChange) -> None:
        if change.team_id is None:
            return
        if not await self._teams.team_exists(conn, change.team_id):
            raise NotFound("team not found", code="team_not_found", param="team_id")
        if change.user_id is not None and not await self._teams.is_member(
            conn, change.team_id, change.user_id
        ):
            raise NotFound(
                "user is not a member of this team", code="member_not_found", param="user_id"
            )

    async def _check_allocation(
        self, conn: AsyncConnection, change: BudgetChange, *, capped: bool
    ) -> None:
        """The team cap binds team leads; strict allocation binds everyone. The cap row is locked
        first, so concurrent changes to one team's limits are checked one after another."""
        if change.team_id is None:
            return
        await self._budgets.lock_allocation(conn, change.team_id, change.period, change.unit)
        strict = (await self._budgets.settings(conn)).allocation == "strict"
        cap = await self._budgets.team_cap(conn, change.team_id, change.period, change.unit)
        if change.scope_type == "member":
            others = 0
            if strict and cap is not None:
                others = await self._budgets.member_total(
                    conn, change.team_id, change.period, change.unit, excluding=change.user_id
                )
            check_member_limit(
                change.limit_amount, team_cap=cap, others=others, strict=strict, capped=capped
            )
        elif strict:
            members = await self._budgets.member_total(
                conn, change.team_id, change.period, change.unit, excluding=None
            )
            check_team_cap(change.limit_amount, members=members)

    async def _write(
        self, conn: AsyncConnection, principal: Principal, change: BudgetChange, meta: RequestMeta
    ) -> tuple[BudgetStatus, bool]:
        budget, old = await self._budgets.upsert(
            conn, org_id=principal.org_id, change=change, actor=principal.user_id
        )
        if old is None:  # a new budget counts what its scope already spent this period
            await self._budgets.seed(conn, budget.id, datetime.now(UTC))
        await self._audit.append(
            conn,
            self._event(
                AuditAction.BUDGET_SET,
                principal,
                "budget",
                str(budget.id),
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
        (status,) = await self._budgets.statuses(conn, at=datetime.now(UTC), budget_id=budget.id)
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
        if budget.user_id == principal.user_id and not self._is_admin(principal, budget.team_id):
            await self._authz.deny(
                principal,
                [Permission.BUDGETS_MANAGE_TEAM],
                target=f"budget:{budget_id}",
                meta=meta,
                team_id=budget.team_id,
                message=_OWN_LIMIT,
                code="own_limit",
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
                    "budget",
                    str(deleted.id),
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
        self,
        principal: Principal,
        *,
        timezone: str | None,
        allocation: Allocation | None,
        meta: RequestMeta,
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
            new = BudgetSettings(timezone or old.timezone, allocation or old.allocation)
            if new.allocation == "strict" and old.allocation != "strict":
                team = await self._budgets.overallocated_team(conn)
                if team is not None:
                    raise InvalidInput(
                        f"the member limits of team {team} exceed its cap; lower them before"
                        " switching to strict allocation",
                        code="allocation_exceeded",
                        param="allocation",
                    )
            if new != old:
                await self._budgets.update_settings(conn, new)
                if new.timezone != old.timezone:
                    # Periods moved. Rebuild every current counter from the ledger before the
                    # new timezone commits, so no reservation reads a period its counter has
                    # not counted yet. Ascending ids: the order reservations lock counters in.
                    now = datetime.now(UTC)
                    for budget_id in await self._budgets.budget_ids(conn):
                        await self._budgets.rebuild_current(conn, budget_id, now)
                await self._audit.append(
                    conn,
                    self._event(
                        AuditAction.SETTINGS_CHANGED,
                        principal,
                        "org",
                        str(principal.org_id),
                        meta,
                        {
                            "setting": "budgets",
                            "old": {"timezone": old.timezone, "allocation": old.allocation},
                            "new": {"timezone": new.timezone, "allocation": new.allocation},
                        },
                    ),
                )
        return new

    async def request_more(
        self, principal: Principal, *, budget_id: UUID, amount: int, reason: str, meta: RequestMeta
    ) -> BudgetRequest:
        """A member asks for more on a budget that applies to them (their own or their team's)."""
        await self._authz.ensure(
            principal, Permission.BUDGET_REQUESTS_CREATE, target="budget_requests", meta=meta
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            budget = await self._budgets.get(conn, budget_id)
            if budget is not None and budget.scope_type == "org":
                raise InvalidInput(
                    "org budgets are changed by org admins directly",
                    code="invalid_budget",
                    param="budget_id",
                )
            if budget is None or not _applies_to(budget, principal):
                raise NotFound("budget not found", code="budget_not_found", param="budget_id")
            created = await self._budgets.create_request(
                conn,
                org_id=principal.org_id,
                budget=budget,
                user_id=principal.user_id,
                amount=amount,
                reason=reason,
            )
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.BUDGET_REQUEST_CREATED,
                    principal,
                    "budget_request",
                    str(created.id),
                    meta,
                    {**_describe(budget), "budget_id": str(budget.id), "amount": amount},
                ),
            )
        return created

    async def list_requests(
        self, principal: Principal, *, status: str | None
    ) -> list[BudgetRequest]:
        """Org admins and finance see every request, leads those of the teams they lead, and
        everyone sees their own."""
        everything = principal.can(Permission.BUDGETS_MANAGE_ORG) or principal.can(
            Permission.REPORTS_READ
        )
        led = [
            t
            for t in principal.led_team_ids()
            if principal.can(Permission.BUDGET_REQUESTS_DECIDE, t)
        ]
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._budgets.list_requests(
                conn, everything=everything, team_ids=led, user_id=principal.user_id, status=status
            )

    async def decide(
        self,
        principal: Principal,
        request_id: UUID,
        *,
        approve: bool,
        grant: GrantType | None,
        note: str | None,
        meta: RequestMeta,
    ) -> BudgetRequest:
        if approve != (grant is not None):
            raise InvalidInput(
                "an approval takes grant_type one_time or permanent; a denial takes none",
                param="grant_type",
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            found = await self._budgets.get_request(conn, request_id)
            budget = await self._budgets.get(conn, found.budget_id) if found else None
        if found is None or budget is None:
            raise NotFound("budget request not found", code="request_not_found")
        needed = (
            Permission.BUDGET_REQUESTS_DECIDE
            if budget.scope_type == "member"
            else Permission.BUDGETS_MANAGE_TEAM
        )
        target = f"budget_request:{request_id}"
        await self._authz.ensure(principal, needed, team_id=found.team_id, target=target, meta=meta)
        admin = self._is_admin(principal, found.team_id)
        if found.user_id == principal.user_id and not admin:
            await self._authz.deny(
                principal,
                [Permission.BUDGETS_MANAGE_TEAM],
                target=target,
                meta=meta,
                team_id=found.team_id,
                message="you cannot decide your own budget request",
                code="own_request",
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            pending = await self._budgets.get_request(conn, request_id, lock=True)
            if pending is None:
                raise NotFound("budget request not found", code="request_not_found")
            if pending.status != "pending":
                raise Conflict("this budget request was already decided", code="request_decided")
            if grant is not None:
                await self._grant(conn, principal, pending, grant, capped=not admin, meta=meta)
            decided = await self._budgets.decide_request(
                conn,
                request_id,
                status="approved" if approve else "denied",
                grant_type=grant,
                actor=principal.user_id,
                note=note,
            )
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.BUDGET_REQUEST_DECIDED,
                    principal,
                    "budget_request",
                    str(request_id),
                    meta,
                    {
                        "budget_id": str(pending.budget_id),
                        "decision": decided.status,
                        "grant_type": grant,
                        "amount": pending.amount,
                        "requested_by": str(pending.user_id),
                    },
                ),
            )
        return decided

    async def _grant(
        self,
        conn: AsyncConnection,
        principal: Principal,
        request: BudgetRequest,
        grant: GrantType,
        *,
        capped: bool,
        meta: RequestMeta,
    ) -> None:
        """One-time: extra budget for the current period only. Permanent: a higher limit, under
        the same cap rules as any other limit change."""
        budget = await self._budgets.get(conn, request.budget_id)
        if budget is None:
            raise NotFound("budget not found", code="budget_not_found")
        if grant == "one_time":
            await self._budgets.grant_once(
                conn, budget_id=budget.id, at=datetime.now(UTC), amount=request.amount
            )
            return
        # Lock as a limit change does (the team's cap, then the budget row) and read the limit
        # again: two approvals at once then add up instead of one overwriting the other.
        if budget.team_id is not None:
            await self._budgets.lock_allocation(conn, budget.team_id, budget.period, budget.unit)
        budget = await self._budgets.get(conn, budget.id, lock=True)
        if budget is None:
            raise NotFound("budget not found", code="budget_not_found")
        change = BudgetChange(
            scope_type=budget.scope_type,
            team_id=budget.team_id,
            user_id=budget.user_id,
            period=budget.period,
            unit=budget.unit,
            limit_amount=budget.limit_amount + request.amount,
            soft_pct=budget.soft_pct,
            reason=f"approved budget request {request.id}",
        )
        await self._check_allocation(conn, change, capped=capped)
        await self._write(conn, principal, change, meta)

    @staticmethod
    def _event(
        action: str,
        principal: Principal,
        target_type: str,
        target_id: str,
        meta: RequestMeta,
        details: dict[str, object],
    ) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_type="user",
            actor_id=principal.user_id,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip=meta.ip,
            request_id=meta.request_id,
        )
