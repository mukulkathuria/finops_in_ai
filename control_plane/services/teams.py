"""Teams and membership (US-02). Org admins manage teams; team leads manage their own members."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError

from control_plane.services.authz import Authorizer
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.identity import IdentityRepository
from gateway.adapters.postgres.teams import MemberRecord, TeamRecord, TeamRepository
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Conflict, NotFound
from gateway.domain.policy import Permission, Principal, Role, RoleGrant
from gateway.domain.teams import TeamRole, clean_team_name


class TeamService:
    def __init__(
        self,
        *,
        db: Database,
        teams: TeamRepository,
        identity: IdentityRepository,
        audit: AuditRepository,
        authz: Authorizer,
    ) -> None:
        self._db = db
        self._teams = teams
        self._identity = identity
        self._audit = audit
        self._authz = authz

    def _ctx(self, principal: Principal) -> TenantContext:
        return TenantContext(principal.org_id, principal.user_id)

    async def create_team(self, principal: Principal, name: str, meta: RequestMeta) -> TeamRecord:
        await self._authz.ensure(principal, Permission.TEAMS_MANAGE, target="teams", meta=meta)
        name = clean_team_name(name)
        try:
            async with self._db.transaction(self._ctx(principal)) as conn:
                team = await self._teams.create_team(
                    conn, org_id=principal.org_id, name=name, created_by=principal.user_id
                )
                await self._audit.append(
                    conn,
                    self._event(AuditAction.TEAM_CREATED, principal, team.id, meta, {"name": name}),
                )
        except IntegrityError as exc:
            raise Conflict(
                "a team with this name already exists", code="team_name_taken", param="name"
            ) from exc
        return team

    async def list_teams(self, principal: Principal) -> list[TeamRecord]:
        see_all = principal.can(Permission.TEAMS_MANAGE) or principal.can(Permission.REPORTS_READ)
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._teams.list_teams(
                conn, only_for_user=None if see_all else principal.user_id
            )

    async def list_members(
        self, principal: Principal, team_id: UUID, meta: RequestMeta
    ) -> list[MemberRecord]:
        async with self._db.transaction(self._ctx(principal)) as conn:
            if not await self._teams.team_exists(conn, team_id):
                raise NotFound("team not found", code="team_not_found")
        if team_id not in principal.team_ids:
            await self._authz.ensure(
                principal,
                Permission.TEAM_MEMBERS_MANAGE,
                team_id=team_id,
                target=f"team:{team_id}",
                meta=meta,
            )
        async with self._db.transaction(self._ctx(principal)) as conn:
            return await self._teams.list_members(conn, team_id)

    async def add_member(
        self, principal: Principal, team_id: UUID, user_id: UUID, role: TeamRole, meta: RequestMeta
    ) -> bool:
        async with self._db.transaction(self._ctx(principal)) as conn:
            if not await self._teams.team_exists(conn, team_id):
                raise NotFound("team not found", code="team_not_found")
            if not await self._teams.user_exists(conn, user_id):
                raise NotFound("user not found", code="user_not_found")
        # Leads add members to their own team; only org admins appoint leads.
        needed = Permission.USERS_MANAGE if role == "lead" else Permission.TEAM_MEMBERS_MANAGE
        await self._authz.ensure(
            principal, needed, team_id=team_id, target=f"team:{team_id}", meta=meta
        )
        async with self._db.transaction(self._ctx(principal)) as conn:
            added = await self._teams.add_member(
                conn,
                org_id=principal.org_id,
                team_id=team_id,
                user_id=user_id,
                added_by=principal.user_id,
            )
            if role == "lead":
                await self._identity.grant_roles(
                    conn,
                    org_id=principal.org_id,
                    user_id=user_id,
                    grants=[RoleGrant(Role.TEAM_LEAD, team_id)],
                    granted_by=principal.user_id,
                )
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.TEAM_MEMBER_ADDED,
                    principal,
                    team_id,
                    meta,
                    {"user_id": str(user_id), "role": role},
                ),
            )
        return added

    async def remove_member(
        self, principal: Principal, team_id: UUID, user_id: UUID, meta: RequestMeta
    ) -> None:
        async with self._db.transaction(self._ctx(principal)) as conn:
            if not await self._teams.team_exists(conn, team_id):
                raise NotFound("team not found", code="team_not_found")
        await self._authz.ensure(
            principal,
            Permission.TEAM_MEMBERS_MANAGE,
            team_id=team_id,
            target=f"team:{team_id}",
            meta=meta,
        )
        if user_id != principal.user_id and not principal.can(Permission.USERS_MANAGE):
            async with self._db.transaction(self._ctx(principal)) as conn:
                lead = await self._teams.is_lead(conn, team_id, user_id)
            if lead:
                await self._authz.deny(
                    principal,
                    [Permission.USERS_MANAGE],
                    target=f"team:{team_id}",
                    meta=meta,
                    team_id=team_id,
                    message="only org admins remove a team lead",
                    code="lead_protected",
                )
        async with self._db.transaction(self._ctx(principal)) as conn:
            if not await self._teams.remove_member(conn, team_id=team_id, user_id=user_id):
                raise NotFound("user is not a member of this team", code="member_not_found")
            await self._audit.append(
                conn,
                self._event(
                    AuditAction.TEAM_MEMBER_REMOVED,
                    principal,
                    team_id,
                    meta,
                    {"user_id": str(user_id)},
                ),
            )

    @staticmethod
    def _event(
        action: str,
        principal: Principal,
        team_id: UUID,
        meta: RequestMeta,
        details: dict[str, object],
    ) -> AuditEvent:
        return AuditEvent(
            action=action,
            actor_type="user",
            actor_id=principal.user_id,
            target_type="team",
            target_id=str(team_id),
            details=details,
            ip=meta.ip,
            request_id=meta.request_id,
        )
