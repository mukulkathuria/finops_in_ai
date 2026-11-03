"""API key persistence and authentication lookups (US-04). Parameterised SQL only."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    id: UUID
    team_id: UUID
    user_id: UUID | None
    name: str
    prefix: str
    scopes: list[str]
    created_by: UUID
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class KeyIdentity:
    key_id: UUID
    org_id: UUID
    team_id: UUID
    user_id: UUID | None
    scopes: frozenset[str]


class ApiKeyRepository:
    async def create(
        self,
        conn: AsyncConnection,
        *,
        org_id: UUID,
        team_id: UUID,
        user_id: UUID | None,
        name: str,
        prefix: str,
        key_hash: str,
        created_by: UUID,
        expires_at: datetime | None,
    ) -> ApiKeyRecord:
        row = await fetch_one(
            conn,
            "INSERT INTO api_keys (org_id, team_id, user_id, name, prefix, key_hash, created_by,"
            " expires_at) VALUES (:org, :team, :user, :name, :prefix, :hash, :by, :exp)"
            " RETURNING id, team_id, user_id, name, prefix, scopes, created_by, created_at,"
            " expires_at, last_used_at, revoked_at",
            {
                "org": org_id,
                "team": team_id,
                "user": user_id,
                "name": name,
                "prefix": prefix,
                "hash": key_hash,
                "by": created_by,
                "exp": expires_at,
            },
        )
        if row is None:
            raise RuntimeError("api key insert returned no row")
        return ApiKeyRecord(**dict(row))

    async def list_for_team(self, conn: AsyncConnection, team_id: UUID) -> list[ApiKeyRecord]:
        rows = await fetch_all(
            conn,
            "SELECT id, team_id, user_id, name, prefix, scopes, created_by, created_at,"
            " expires_at, last_used_at, revoked_at FROM api_keys"
            " WHERE team_id = :t ORDER BY created_at DESC",
            {"t": team_id},
        )
        return [ApiKeyRecord(**dict(r)) for r in rows]

    async def revoke(self, conn: AsyncConnection, *, team_id: UUID, key_id: UUID, by: UUID) -> str:
        """'revoked', 'already_revoked' or 'not_found' (keys of other teams count as not found)."""
        params = {"id": key_id, "t": team_id, "by": by}
        revoked = await fetch_one(
            conn,
            "UPDATE api_keys SET revoked_at = now(), revoked_by = :by"
            " WHERE id = :id AND team_id = :t AND revoked_at IS NULL RETURNING id",
            params,
        )
        if revoked is not None:
            return "revoked"
        exists = await fetch_one(
            conn, "SELECT 1 FROM api_keys WHERE id = :id AND team_id = :t", params
        )
        return "already_revoked" if exists else "not_found"

    async def authenticate(self, conn: AsyncConnection, key_hash: str) -> KeyIdentity | None:
        """Pre-tenant lookup by digest. A key works only while it is unrevoked, unexpired, its team
        exists and its bound member (if any) is active and still in that team. No cache: a revoked
        key fails on the very next request."""
        row = await fetch_one(
            conn,
            "SELECT k.id, k.org_id, k.team_id, k.user_id, k.scopes FROM api_keys k"
            " WHERE k.key_hash = :h AND k.revoked_at IS NULL"
            " AND (k.expires_at IS NULL OR k.expires_at > now())",
            {"h": key_hash},
        )
        if row is None:
            return None
        return KeyIdentity(
            key_id=row["id"],
            org_id=row["org_id"],
            team_id=row["team_id"],
            user_id=row["user_id"],
            scopes=frozenset(row["scopes"]),
        )

    async def bound_member_is_active(
        self, conn: AsyncConnection, *, team_id: UUID, user_id: UUID
    ) -> bool:
        """Must run in the key's tenant context."""
        row = await fetch_one(
            conn,
            "SELECT 1 FROM users u JOIN team_members m ON m.user_id = u.id AND m.team_id = :t"
            " WHERE u.id = :u AND u.status = 'active'",
            {"t": team_id, "u": user_id},
        )
        return row is not None

    async def touch(self, conn: AsyncConnection, key_id: UUID) -> None:
        """Record use at most once a minute, so hot keys do not write on every request."""
        await conn.exec_driver_sql(
            "UPDATE api_keys SET last_used_at = now() WHERE id = %(id)s"
            " AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
            {"id": key_id},
        )
