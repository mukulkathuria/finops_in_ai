"""Invitations (US-02): single-use, expiring links; acceptance sets the password and joins."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid7

from sqlalchemy.exc import IntegrityError

from control_plane.services.authz import Authorizer
from control_plane.services.identity import raise_for_constraint, require_strong_password
from control_plane.services.sessions import Session, SessionService
from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext, set_lookup
from gateway.adapters.postgres.identity import IdentityRepository
from gateway.adapters.postgres.teams import InvitationRecord, InvitationRepository, TeamRepository
from gateway.adapters.security.passwords import Argon2PasswordHasher
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
from gateway.domain.errors import Conflict, Gone, InvalidInput, NotFound
from gateway.domain.identity import normalize_email
from gateway.domain.policy import Permission, Principal, Role, RoleGrant
from gateway.domain.secrets import digest, new_secret
from gateway.domain.teams import TeamRole

OrgRole = Literal["org_admin", "finance", "auditor", "member"]


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    invitation: InvitationRecord
    token: str
    invite_url: str


class InvitationService:
    def __init__(
        self,
        *,
        db: Database,
        invitations: InvitationRepository,
        teams: TeamRepository,
        identity: IdentityRepository,
        audit: AuditRepository,
        authz: Authorizer,
        hasher: Argon2PasswordHasher,
        sessions: SessionService,
        ttl: timedelta,
        web_base_url: str,
    ) -> None:
        self._db = db
        self._invitations = invitations
        self._teams = teams
        self._identity = identity
        self._audit = audit
        self._authz = authz
        self._hasher = hasher
        self._sessions = sessions
        self._ttl = ttl
        self._web_base_url = web_base_url.rstrip("/")

    async def invite(
        self,
        principal: Principal,
        *,
        email: str,
        org_role: OrgRole,
        team_id: UUID | None,
        team_role: TeamRole | None,
        meta: RequestMeta,
    ) -> CreatedInvitation:
        email = normalize_email(email)
        if (team_id is None) != (team_role is None):
            raise InvalidInput("team_id and team_role go together", code="invalid_team_assignment")
        # Authorise first, so a caller without the right learns nothing about teams or accounts.
        # Org admins invite anyone; team leads may only invite plain members into their team.
        if not principal.can(Permission.USERS_MANAGE):
            if team_id is None or org_role != "member" or team_role != "member":
                await self._authz.deny(
                    principal, [Permission.USERS_MANAGE], target="invitations", meta=meta
                )
            await self._authz.ensure(
                principal,
                Permission.TEAM_MEMBERS_MANAGE,
                team_id=team_id,
                target=f"team:{team_id}",
                meta=meta,
            )
        ctx = TenantContext(principal.org_id, principal.user_id)
        async with self._db.transaction(ctx) as conn:
            if team_id and not await self._teams.team_exists(conn, team_id):
                raise NotFound("team not found", code="team_not_found")
            if await self._teams.email_in_org(conn, email):
                raise Conflict(
                    "this person already has an account", code="email_taken", param="email"
                )
        token = new_secret()
        async with self._db.transaction(ctx) as conn:
            await self._invitations.supersede_pending(conn, email)
            record = await self._invitations.create(
                conn,
                org_id=principal.org_id,
                email=email,
                org_role=org_role,
                team_id=team_id,
                team_role=team_role,
                token_hash=digest(token),
                invited_by=principal.user_id,
                expires_at=datetime.now(UTC) + self._ttl,
            )
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.USER_INVITED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="invitation",
                    target_id=str(record.id),
                    details={"org_role": org_role, "team_id": str(team_id) if team_id else None},
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )
        # The token travels in the URL fragment, so it never reaches server logs or referrers.
        url = f"{self._web_base_url}/accept-invite#token={token}"
        return CreatedInvitation(invitation=record, token=token, invite_url=url)

    async def list_pending(self, principal: Principal) -> list[InvitationRecord]:
        scope = None if principal.can(Permission.USERS_MANAGE) else principal.led_team_ids()
        async with self._db.transaction(TenantContext(principal.org_id, principal.user_id)) as conn:
            return await self._invitations.list_pending(conn, team_ids=scope)

    async def revoke(self, principal: Principal, invitation_id: UUID, meta: RequestMeta) -> None:
        ctx = TenantContext(principal.org_id, principal.user_id)
        async with self._db.transaction(ctx) as conn:
            record = await self._invitations.get(conn, invitation_id)
        if record is None:
            raise NotFound("invitation not found", code="invitation_not_found")
        if not principal.can(Permission.USERS_MANAGE):
            await self._authz.ensure(
                principal,
                Permission.TEAM_MEMBERS_MANAGE,
                team_id=record.team_id,
                target=f"invitation:{invitation_id}",
                meta=meta,
            )
        async with self._db.transaction(ctx) as conn:
            if not await self._invitations.revoke(conn, invitation_id):
                raise Gone("invitation was already used or revoked", code="invitation_closed")
            await self._audit.append(
                conn,
                AuditEvent(
                    action=AuditAction.INVITATION_REVOKED,
                    actor_type="user",
                    actor_id=principal.user_id,
                    target_type="invitation",
                    target_id=str(invitation_id),
                    ip=meta.ip,
                    request_id=meta.request_id,
                ),
            )

    async def accept(
        self, *, token: str, password: str, display_name: str, meta: RequestMeta
    ) -> Session:
        token_hash = digest(token)
        async with self._db.transaction() as conn:
            await set_lookup(conn, "token_hash", token_hash)
            record = await self._invitations.find_by_token_hash(conn, token_hash)
        if record is None:
            raise NotFound("invitation link is not valid", code="invitation_invalid")
        if record.used_at is not None:
            raise Gone("invitation link was already used", code="invitation_used")
        if record.revoked_at is not None:
            raise Gone("invitation link was revoked", code="invitation_revoked")
        if record.expires_at <= datetime.now(UTC):
            raise Gone("invitation link has expired", code="invitation_expired")
        require_strong_password(password, email=record.email)
        password_hash = await self._hasher.hash(password)
        user_id = uuid7()
        grants = [RoleGrant(record.org_role)]
        if record.org_role == Role.ORG_ADMIN:
            grants.append(RoleGrant(Role.MEMBER))
        if record.team_id and record.team_role == "lead":
            grants.append(RoleGrant(Role.TEAM_LEAD, record.team_id))
        try:
            async with self._db.transaction(TenantContext(record.org_id, user_id)) as conn:
                if not await self._invitations.consume(conn, record.id):
                    raise Gone("invitation link was already used", code="invitation_used")
                await self._identity.insert_user(
                    conn,
                    user_id=user_id,
                    org_id=record.org_id,
                    email=record.email,
                    display_name=display_name.strip(),
                    password_hash=password_hash,
                )
                await self._identity.grant_roles(
                    conn, org_id=record.org_id, user_id=user_id, grants=grants, granted_by=None
                )
                if record.team_id:
                    await self._teams.add_member(
                        conn,
                        org_id=record.org_id,
                        team_id=record.team_id,
                        user_id=user_id,
                        added_by=user_id,
                    )
                await self._audit.append(
                    conn,
                    AuditEvent(
                        action=AuditAction.INVITATION_ACCEPTED,
                        actor_type="user",
                        actor_id=user_id,
                        target_type="invitation",
                        target_id=str(record.id),
                        details={"roles": [g.role for g in grants]},
                        ip=meta.ip,
                        request_id=meta.request_id,
                    ),
                )
                session = await self._sessions.issue(conn, user_id=user_id)
        except IntegrityError as exc:
            raise_for_constraint(exc)
            raise
        return session
