"""Identity persistence: orgs, users and role grants. Raw parameterised SQL (spec section 2)."""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.domain.policy import RoleCatalog, RoleGrant


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: UUID
    org_id: UUID
    email: str
    status: str
    grants: tuple[RoleGrant, ...]
    team_ids: frozenset[UUID]


class IdentityRepository:
    async def claim_bootstrap(self, conn: AsyncConnection) -> bool:
        """Atomically flip the one-time bootstrap flag. False means bootstrap already happened."""
        row = await fetch_one(
            conn,
            "UPDATE system_state SET value = 'true', updated_at = now()"
            " WHERE key = 'bootstrap_completed' AND value = 'false' RETURNING key",
        )
        return row is not None

    async def insert_org(
        self, conn: AsyncConnection, *, org_id: UUID, name: str, slug: str
    ) -> None:
        await conn.exec_driver_sql(
            "INSERT INTO orgs (id, name, slug) VALUES (%(id)s, %(name)s, %(slug)s)",
            {"id": org_id, "name": name, "slug": slug},
        )

    async def insert_user(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        org_id: UUID,
        email: str,
        display_name: str,
        password_hash: str | None,
        status: str = "active",
    ) -> None:
        await conn.exec_driver_sql(
            "INSERT INTO users (id, org_id, email, display_name, password_hash, status)"
            " VALUES (%(id)s, %(org)s, %(email)s, %(name)s, %(hash)s, %(status)s)",
            {
                "id": user_id,
                "org": org_id,
                "email": email,
                "name": display_name,
                "hash": password_hash,
                "status": status,
            },
        )

    async def grant_roles(
        self,
        conn: AsyncConnection,
        *,
        org_id: UUID,
        user_id: UUID,
        grants: Sequence[RoleGrant],
        granted_by: UUID | None = None,
    ) -> None:
        for grant in grants:
            await conn.exec_driver_sql(
                "INSERT INTO role_assignments (org_id, user_id, role, team_id, granted_by)"
                " VALUES (%(org)s, %(user)s, %(role)s, %(team)s, %(by)s)"
                " ON CONFLICT DO NOTHING",
                {
                    "org": org_id,
                    "user": user_id,
                    "role": grant.role,
                    "team": grant.team_id,
                    "by": granted_by,
                },
            )

    async def load_user(self, conn: AsyncConnection, user_id: UUID) -> UserRecord | None:
        row = await fetch_one(
            conn,
            "SELECT u.id, u.org_id, u.email, u.status,"
            " COALESCE((SELECT jsonb_agg(jsonb_build_object("
            "             'role', ra.role, 'team_id', ra.team_id))"
            "           FROM role_assignments ra WHERE ra.user_id = u.id), '[]'::jsonb) AS grants"
            " FROM users u WHERE u.id = :uid",
            {"uid": user_id},
        )
        if row is None:
            return None
        grants = tuple(
            RoleGrant(g["role"], UUID(g["team_id"]) if g["team_id"] else None)
            for g in row["grants"]
        )
        return UserRecord(
            id=row["id"],
            org_id=row["org_id"],
            email=row["email"],
            status=row["status"],
            grants=grants,
            team_ids=frozenset(),
        )

    async def load_role_catalog(self, conn: AsyncConnection) -> RoleCatalog:
        rows = await fetch_all(conn, "SELECT role, permission FROM role_permissions")
        catalog: dict[str, set[str]] = {}
        for r in rows:
            catalog.setdefault(r["role"], set()).add(r["permission"])
        return {role: frozenset(perms) for role, perms in catalog.items()}


class RoleCatalogCache:
    """Role permissions change only through migrations, so cache them per process with a TTL."""

    def __init__(self, repo: IdentityRepository, ttl_seconds: float = 300.0) -> None:
        self._repo = repo
        self._ttl = ttl_seconds
        self._catalog: RoleCatalog | None = None
        self._loaded_at = 0.0

    async def get(self, conn: AsyncConnection) -> RoleCatalog:
        now = time.monotonic()
        if self._catalog is None or now - self._loaded_at > self._ttl:
            self._catalog = await self._repo.load_role_catalog(conn)
            self._loaded_at = now
        return self._catalog
