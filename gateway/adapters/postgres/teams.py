"""Teams, membership and invitations persistence (US-02). Parameterised SQL only."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class TeamRecord:
    id: UUID
    name: str
    created_at: datetime
    member_count: int


@dataclass(frozen=True, slots=True)
class MemberRecord:
    user_id: UUID
    email: str
    display_name: str
    is_lead: bool


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    id: UUID
    org_id: UUID
    email: str
    org_role: str
    team_id: UUID | None
    team_role: str | None
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class TeamRepository:
    async def create_team(
        self, conn: AsyncConnection, *, org_id: UUID, name: str, created_by: UUID
    ) -> TeamRecord:
        row = await fetch_one(
            conn,
            "INSERT INTO teams (org_id, name, created_by) VALUES (:org, :name, :by)"
            " RETURNING id, name, created_at, 0 AS member_count",
            {"org": org_id, "name": name, "by": created_by},
        )
        if row is None:
            raise RuntimeError("team insert returned no row")
        return TeamRecord(**dict(row))

    async def list_teams(
        self, conn: AsyncConnection, *, only_for_user: UUID | None = None
    ) -> list[TeamRecord]:
        rows = await fetch_all(
            conn,
            "SELECT t.id, t.name, t.created_at,"
            " (SELECT count(*) FROM team_members m WHERE m.team_id = t.id) AS member_count"
            " FROM teams t"
            " WHERE CAST(:user AS uuid) IS NULL OR EXISTS"
            "  (SELECT 1 FROM team_members m WHERE m.team_id = t.id AND m.user_id = :user)"
            " ORDER BY lower(t.name)",
            {"user": only_for_user},
        )
        return [TeamRecord(**dict(r)) for r in rows]

    async def team_exists(self, conn: AsyncConnection, team_id: UUID) -> bool:
        return (
            await fetch_one(conn, "SELECT 1 FROM teams WHERE id = :t", {"t": team_id}) is not None
        )

    async def user_exists(self, conn: AsyncConnection, user_id: UUID) -> bool:
        return (
            await fetch_one(conn, "SELECT 1 FROM users WHERE id = :u", {"u": user_id}) is not None
        )

    async def is_member(self, conn: AsyncConnection, team_id: UUID, user_id: UUID) -> bool:
        row = await fetch_one(
            conn,
            "SELECT 1 FROM team_members WHERE team_id = :t AND user_id = :u",
            {"t": team_id, "u": user_id},
        )
        return row is not None

    async def email_in_org(self, conn: AsyncConnection, email: str) -> bool:
        row = await fetch_one(
            conn,
            "SELECT 1 FROM users WHERE email = :e AND org_id = app_current_org()",
            {"e": email},
        )
        return row is not None

    async def add_member(
        self, conn: AsyncConnection, *, org_id: UUID, team_id: UUID, user_id: UUID, added_by: UUID
    ) -> bool:
        """True when newly added; False when the user already belonged to the team."""
        row = await fetch_one(
            conn,
            "INSERT INTO team_members (org_id, team_id, user_id, added_by)"
            " VALUES (:org, :team, :user, :by) ON CONFLICT DO NOTHING RETURNING user_id",
            {"org": org_id, "team": team_id, "user": user_id, "by": added_by},
        )
        return row is not None

    async def remove_member(self, conn: AsyncConnection, *, team_id: UUID, user_id: UUID) -> bool:
        await conn.exec_driver_sql(
            "DELETE FROM role_assignments WHERE team_id = %(t)s AND user_id = %(u)s",
            {"t": team_id, "u": user_id},
        )
        row = await fetch_one(
            conn,
            "DELETE FROM team_members WHERE team_id = :t AND user_id = :u RETURNING user_id",
            {"t": team_id, "u": user_id},
        )
        return row is not None

    async def list_members(self, conn: AsyncConnection, team_id: UUID) -> list[MemberRecord]:
        rows = await fetch_all(
            conn,
            "SELECT u.id AS user_id, u.email, u.display_name,"
            " EXISTS (SELECT 1 FROM role_assignments ra WHERE ra.user_id = u.id"
            "         AND ra.role = 'team_lead' AND ra.team_id = m.team_id) AS is_lead"
            " FROM team_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.team_id = :t ORDER BY u.email",
            {"t": team_id},
        )
        return [MemberRecord(**dict(r)) for r in rows]


class InvitationRepository:
    async def supersede_pending(self, conn: AsyncConnection, email: str) -> None:
        await conn.exec_driver_sql(
            "UPDATE invitations SET revoked_at = now()"
            " WHERE email = %(e)s AND used_at IS NULL AND revoked_at IS NULL",
            {"e": email},
        )

    async def create(
        self,
        conn: AsyncConnection,
        *,
        org_id: UUID,
        email: str,
        org_role: str,
        team_id: UUID | None,
        team_role: str | None,
        token_hash: str,
        invited_by: UUID,
        expires_at: datetime,
    ) -> InvitationRecord:
        row = await fetch_one(
            conn,
            "INSERT INTO invitations (org_id, email, org_role, team_id, team_role, token_hash,"
            " invited_by, expires_at) VALUES (:org, :email, :role, :team, :team_role, :hash, :by,"
            " :exp) RETURNING id, org_id, email, org_role, team_id, team_role, expires_at,"
            " used_at, revoked_at, created_at",
            {
                "org": org_id,
                "email": email,
                "role": org_role,
                "team": team_id,
                "team_role": team_role,
                "hash": token_hash,
                "by": invited_by,
                "exp": expires_at,
            },
        )
        if row is None:
            raise RuntimeError("invitation insert returned no row")
        return InvitationRecord(**dict(row))

    async def list_pending(
        self, conn: AsyncConnection, *, team_ids: frozenset[UUID] | None = None
    ) -> list[InvitationRecord]:
        rows = await fetch_all(
            conn,
            "SELECT id, org_id, email, org_role, team_id, team_role, expires_at, used_at,"
            " revoked_at, created_at FROM invitations"
            " WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > now()"
            " AND (CAST(:all AS boolean) OR team_id = ANY(:teams)) ORDER BY created_at DESC",
            {"all": team_ids is None, "teams": list(team_ids or ())},
        )
        return [InvitationRecord(**dict(r)) for r in rows]

    async def get(self, conn: AsyncConnection, invitation_id: UUID) -> InvitationRecord | None:
        row = await fetch_one(
            conn,
            "SELECT id, org_id, email, org_role, team_id, team_role, expires_at, used_at,"
            " revoked_at, created_at FROM invitations WHERE id = :id",
            {"id": invitation_id},
        )
        return InvitationRecord(**dict(row)) if row else None

    async def revoke(self, conn: AsyncConnection, invitation_id: UUID) -> bool:
        row = await fetch_one(
            conn,
            "UPDATE invitations SET revoked_at = now()"
            " WHERE id = :id AND used_at IS NULL AND revoked_at IS NULL RETURNING id",
            {"id": invitation_id},
        )
        return row is not None

    async def find_by_token_hash(
        self, conn: AsyncConnection, token_hash: str
    ) -> InvitationRecord | None:
        """Pre-tenant read: the caller must have set the token_hash lookup in this transaction."""
        row = await fetch_one(
            conn,
            "SELECT id, org_id, email, org_role, team_id, team_role, expires_at, used_at,"
            " revoked_at, created_at FROM invitations WHERE token_hash = :h",
            {"h": token_hash},
        )
        return InvitationRecord(**dict(row)) if row else None

    async def consume(self, conn: AsyncConnection, invitation_id: UUID) -> bool:
        """Atomic single use: exactly one concurrent caller wins."""
        row = await fetch_one(
            conn,
            "UPDATE invitations SET used_at = now() WHERE id = :id AND used_at IS NULL"
            " AND revoked_at IS NULL AND expires_at > now() RETURNING id",
            {"id": invitation_id},
        )
        return row is not None
