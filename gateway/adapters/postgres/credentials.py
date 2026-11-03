"""Encrypted provider credentials (US-06). Plaintext keys exist only in memory, briefly."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one
from gateway.adapters.security.envelope import Envelope, Sealed

OrgKeys = dict[str, tuple[UUID, str]]  # provider -> (credential id, decrypted key)


@dataclass(frozen=True, slots=True)
class CredentialInfo:
    id: UUID
    provider: str
    label: str
    key_hint: str
    created_at: datetime


def credential_aad(org_id: UUID, provider: str, credential_id: UUID) -> bytes:
    return f"finops:provider-key:{org_id}:{provider}:{credential_id}".encode()


class CredentialRepository:
    async def revoke_active(self, conn: AsyncConnection, provider: str) -> bool:
        row = await fetch_one(
            conn,
            "UPDATE provider_credentials SET revoked_at = now()"
            " WHERE provider = :p AND revoked_at IS NULL RETURNING id",
            {"p": provider},
        )
        return row is not None

    async def insert(
        self,
        conn: AsyncConnection,
        *,
        credential_id: UUID,
        org_id: UUID,
        provider: str,
        label: str,
        sealed: Sealed,
        key_hint: str,
        created_by: UUID,
    ) -> CredentialInfo:
        row = await fetch_one(
            conn,
            "INSERT INTO provider_credentials (id, org_id, provider, label, ciphertext,"
            " wrapped_key, key_id, key_hint, created_by)"
            " VALUES (:id, :org, :p, :label, :ct, :wk, :kid, :hint, :by)"
            " RETURNING id, provider, label, key_hint, created_at",
            {
                "id": credential_id,
                "org": org_id,
                "p": provider,
                "label": label,
                "ct": sealed.ciphertext,
                "wk": sealed.wrapped_key,
                "kid": sealed.key_id,
                "hint": key_hint,
                "by": created_by,
            },
        )
        if row is None:
            raise RuntimeError("credential insert returned no row")
        return CredentialInfo(**dict(row))

    async def list_active(self, conn: AsyncConnection) -> list[CredentialInfo]:
        rows = await fetch_all(
            conn,
            "SELECT id, provider, label, key_hint, created_at FROM provider_credentials"
            " WHERE revoked_at IS NULL ORDER BY provider",
        )
        return [CredentialInfo(**dict(r)) for r in rows]

    async def active_sealed(self, conn: AsyncConnection) -> list[tuple[UUID, str, Sealed]]:
        rows = await fetch_all(
            conn,
            "SELECT id, provider, ciphertext, wrapped_key, key_id FROM provider_credentials"
            " WHERE revoked_at IS NULL",
        )
        return [
            (
                r["id"],
                r["provider"],
                Sealed(bytes(r["ciphertext"]), bytes(r["wrapped_key"]), r["key_id"]),
            )
            for r in rows
        ]


def org_key_lookup(
    db: Database, repo: CredentialRepository, envelope: Envelope
) -> Callable[[UUID], Awaitable[OrgKeys]]:
    """An org's decrypted provider keys, for building that org's adapters."""

    async def lookup(org_id: UUID) -> OrgKeys:
        async with db.transaction(TenantContext(org_id)) as conn:
            sealed = await repo.active_sealed(conn)
        return {p: (cid, envelope.open(s, credential_aad(org_id, p, cid))) for cid, p, s in sealed}

    return lookup
