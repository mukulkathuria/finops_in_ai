"""Refresh token families and login bookkeeping (US-03). Parameterised SQL only."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_one


@dataclass(frozen=True, slots=True)
class RefreshRecord:
    id: UUID
    org_id: UUID
    user_id: UUID
    family_id: UUID
    expires_at: datetime
    family_expires_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class LoginRecord:
    id: UUID
    org_id: UUID
    email: str
    status: str
    password_hash: str | None
    failed_logins: int
    locked_until: datetime | None


class SessionRepository:
    async def create_refresh(
        self,
        conn: AsyncConnection,
        *,
        org_id: UUID,
        user_id: UUID,
        family_id: UUID,
        token_hash: str,
        expires_at: datetime,
        family_expires_at: datetime,
    ) -> None:
        await conn.exec_driver_sql(
            "INSERT INTO refresh_tokens (org_id, user_id, family_id, token_hash, expires_at,"
            " family_expires_at)"
            " VALUES (%(org)s, %(user)s, %(family)s, %(hash)s, %(exp)s, %(fexp)s)",
            {
                "org": org_id,
                "user": user_id,
                "family": family_id,
                "hash": token_hash,
                "exp": expires_at,
                "fexp": family_expires_at,
            },
        )

    async def find_refresh(self, conn: AsyncConnection, token_hash: str) -> RefreshRecord | None:
        """Pre-tenant read: the token_hash lookup must be set in this transaction."""
        row = await fetch_one(
            conn,
            "SELECT id, org_id, user_id, family_id, expires_at, family_expires_at, rotated_at,"
            " revoked_at FROM refresh_tokens WHERE token_hash = :h",
            {"h": token_hash},
        )
        return RefreshRecord(**dict(row)) if row else None

    async def mark_rotated(self, conn: AsyncConnection, token_id: UUID) -> bool:
        """Atomic: only one concurrent refresh with the same token can rotate it."""
        row = await fetch_one(
            conn,
            "UPDATE refresh_tokens SET rotated_at = now() WHERE id = :id"
            " AND rotated_at IS NULL AND revoked_at IS NULL RETURNING id",
            {"id": token_id},
        )
        return row is not None

    async def revoke_family(self, conn: AsyncConnection, family_id: UUID, reason: str) -> int:
        result = await conn.exec_driver_sql(
            "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = %(reason)s"
            " WHERE family_id = %(family)s AND revoked_at IS NULL",
            {"family": family_id, "reason": reason},
        )
        return int(result.rowcount)

    async def find_login(self, conn: AsyncConnection, email: str) -> LoginRecord | None:
        """Pre-tenant read: the email lookup must be set in this transaction."""
        row = await fetch_one(
            conn,
            "SELECT id, org_id, email, status, password_hash, failed_logins, locked_until"
            " FROM users WHERE email = :e",
            {"e": email},
        )
        return LoginRecord(**dict(row)) if row else None

    async def record_failure(
        self, conn: AsyncConnection, user_id: UUID, *, threshold: int
    ) -> tuple[int, datetime | None]:
        """Increment the failure count atomically and start or extend the backoff lock."""
        row = await fetch_one(
            conn,
            "UPDATE users SET failed_logins = failed_logins + 1,"
            " locked_until = CASE WHEN failed_logins + 1 >= :t"
            "   THEN now() + least(interval '1 minute' * power(2, failed_logins + 1 - :t),"
            "                      interval '1 hour')"
            "   ELSE locked_until END"
            " WHERE id = :id RETURNING failed_logins, locked_until",
            {"id": user_id, "t": threshold},
        )
        if row is None:
            raise RuntimeError("login user disappeared during failure accounting")
        return int(row["failed_logins"]), row["locked_until"]

    async def record_success(
        self, conn: AsyncConnection, user_id: UUID, new_hash: str | None = None
    ) -> None:
        await conn.exec_driver_sql(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = now(),"
            " password_hash = COALESCE(%(hash)s, password_hash) WHERE id = %(id)s",
            {"id": user_id, "hash": new_hash},
        )
