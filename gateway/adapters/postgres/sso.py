"""The single sign-on tables (US-27, migration 0028): an org's provider, the identities it
maps to users, and the single-use secrets of sign-ins in progress."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_one
from gateway.adapters.security.envelope import Sealed


def sso_aad(org_id: UUID) -> bytes:
    return f"finops:sso-client-secret:{org_id}".encode()


@dataclass(frozen=True, slots=True)
class SsoConfig:
    org_id: UUID
    issuer: str
    client_id: str
    secret: Sealed | None
    allowed_domains: tuple[str, ...]
    local_login_disabled: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """A single-use secret found by its hash: a sign-in's state, or a one-time code."""

    org_id: UUID
    kind: str
    nonce: str | None
    code_verifier: str | None
    user_id: UUID | None
    expired: bool


_CONFIG: Final = """
SELECT org_id, issuer, client_id, secret_ciphertext, secret_wrapped_key, secret_key_id,
       allowed_domains, local_login_disabled, updated_at
FROM org_sso WHERE org_id = app_current_org()
"""
_SAVE_CONFIG: Final = """
INSERT INTO org_sso (org_id, issuer, client_id, secret_ciphertext, secret_wrapped_key,
                     secret_key_id, allowed_domains, local_login_disabled, updated_by, updated_at)
VALUES (app_current_org(), :issuer, :client_id, :ciphertext, :wrapped, :key_id,
        CAST(:domains AS text[]), :disabled, :by, now())
ON CONFLICT (org_id) DO UPDATE
  SET issuer = excluded.issuer, client_id = excluded.client_id,
      secret_ciphertext = excluded.secret_ciphertext,
      secret_wrapped_key = excluded.secret_wrapped_key, secret_key_id = excluded.secret_key_id,
      allowed_domains = excluded.allowed_domains,
      local_login_disabled = excluded.local_login_disabled,
      updated_by = excluded.updated_by, updated_at = now()
RETURNING org_id, issuer, client_id, secret_ciphertext, secret_wrapped_key, secret_key_id,
       allowed_domains, local_login_disabled, updated_at
"""
_DELETE_CONFIG: Final = "DELETE FROM org_sso WHERE org_id = app_current_org() RETURNING org_id"
_LOCAL_DISABLED: Final = "SELECT local_login_disabled FROM org_sso WHERE org_id = app_current_org()"
_ORG_BY_SLUG: Final = "SELECT id FROM orgs WHERE slug = app_lookup('org_slug')"
_PURGE: Final = "DELETE FROM sso_logins WHERE org_id = app_current_org() AND expires_at < now()"
_SAVE_LOGIN: Final = """
INSERT INTO sso_logins (secret_hash, org_id, kind, nonce, code_verifier, user_id, expires_at)
VALUES (:hash, app_current_org(), :kind, :nonce, :verifier, :user,
        now() + make_interval(secs => :ttl))
"""
_FIND_LOGIN: Final = """
SELECT org_id, kind, nonce, code_verifier, user_id, (expires_at < now()) AS expired
FROM sso_logins WHERE secret_hash = app_lookup('sso_secret')
"""
_TAKE_LOGIN: Final = """
DELETE FROM sso_logins WHERE secret_hash = :hash AND org_id = app_current_org()
RETURNING secret_hash
"""
_IDENTITY: Final = """
SELECT user_id FROM identities WHERE provider = :provider AND subject = :subject
"""
_TOUCH_IDENTITY: Final = """
UPDATE identities SET last_login_at = now(), email = coalesce(:email, email)
WHERE provider = :provider AND subject = :subject
"""
_LINK: Final = """
INSERT INTO identities (org_id, user_id, provider, subject, email, last_login_at)
VALUES (app_current_org(), :user, :provider, :subject, :email, now())
ON CONFLICT (org_id, provider, subject) DO NOTHING
RETURNING user_id
"""
_USER_BY_EMAIL: Final = "SELECT id FROM users WHERE email = :email"


def _config(row: RowMapping) -> SsoConfig:
    sealed = None
    if row["secret_ciphertext"] is not None:
        sealed = Sealed(
            bytes(row["secret_ciphertext"]),
            bytes(row["secret_wrapped_key"]),
            str(row["secret_key_id"]),
        )
    return SsoConfig(
        row["org_id"],
        row["issuer"],
        row["client_id"],
        sealed,
        tuple(row["allowed_domains"]),
        bool(row["local_login_disabled"]),
        row["updated_at"],
    )


class SsoRepository:
    async def config(self, conn: AsyncConnection) -> SsoConfig | None:
        row = await fetch_one(conn, _CONFIG, {})
        return _config(row) if row is not None else None

    async def save_config(
        self,
        conn: AsyncConnection,
        *,
        issuer: str,
        client_id: str,
        secret: Sealed | None,
        domains: list[str],
        local_login_disabled: bool,
        by: UUID,
    ) -> SsoConfig:
        params = {
            "issuer": issuer,
            "client_id": client_id,
            "ciphertext": secret.ciphertext if secret else None,
            "wrapped": secret.wrapped_key if secret else None,
            "key_id": secret.key_id if secret else None,
            "domains": domains,
            "disabled": local_login_disabled,
            "by": by,
        }
        row = await fetch_one(conn, _SAVE_CONFIG, params)
        if row is None:
            raise RuntimeError("single sign-on upsert returned no row")
        return _config(row)

    async def delete_config(self, conn: AsyncConnection) -> bool:
        return await fetch_one(conn, _DELETE_CONFIG, {}) is not None

    async def local_login_disabled(self, conn: AsyncConnection) -> bool:
        """Whether the org in the tenant context has turned local login off."""
        row = await fetch_one(conn, _LOCAL_DISABLED, {})
        return row is not None and bool(row["local_login_disabled"])

    async def org_by_slug(self, conn: AsyncConnection) -> UUID | None:
        """The org whose slug the `org_slug` lookup holds (before any tenant context)."""
        row = await fetch_one(conn, _ORG_BY_SLUG, {})
        return row["id"] if row is not None else None

    async def purge(self, conn: AsyncConnection) -> None:
        await conn.execute(text(_PURGE))

    async def save_login(
        self,
        conn: AsyncConnection,
        *,
        secret_hash: str,
        kind: str,
        ttl_s: int,
        nonce: str | None = None,
        verifier: str | None = None,
        user_id: UUID | None = None,
    ) -> None:
        params = {
            "hash": secret_hash,
            "kind": kind,
            "nonce": nonce,
            "verifier": verifier,
            "user": user_id,
            "ttl": ttl_s,
        }
        await conn.execute(text(_SAVE_LOGIN), params)

    async def find_login(self, conn: AsyncConnection) -> PendingLogin | None:
        """The secret the `sso_secret` lookup holds the hash of (before any tenant context)."""
        row = await fetch_one(conn, _FIND_LOGIN, {})
        if row is None:
            return None
        return PendingLogin(
            row["org_id"],
            row["kind"],
            row["nonce"],
            row["code_verifier"],
            row["user_id"],
            bool(row["expired"]),
        )

    async def take_login(self, conn: AsyncConnection, secret_hash: str) -> bool:
        """Delete the secret (single use); False when another request took it first."""
        return await fetch_one(conn, _TAKE_LOGIN, {"hash": secret_hash}) is not None

    async def identity(self, conn: AsyncConnection, *, provider: str, subject: str) -> UUID | None:
        row = await fetch_one(conn, _IDENTITY, {"provider": provider, "subject": subject})
        return row["user_id"] if row is not None else None

    async def touch_identity(
        self, conn: AsyncConnection, *, provider: str, subject: str, email: str | None
    ) -> None:
        params = {"provider": provider, "subject": subject, "email": email}
        await conn.execute(text(_TOUCH_IDENTITY), params)

    async def link(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        provider: str,
        subject: str,
        email: str | None,
    ) -> UUID | None:
        """Map the identity to the user; None when a concurrent sign-in linked it first."""
        params = {"user": user_id, "provider": provider, "subject": subject, "email": email}
        row = await fetch_one(conn, _LINK, params)
        return row["user_id"] if row is not None else None

    async def user_by_email(self, conn: AsyncConnection, email: str) -> UUID | None:
        row = await fetch_one(conn, _USER_BY_EMAIL, {"email": email})
        return row["id"] if row is not None else None
