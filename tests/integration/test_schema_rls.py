"""Schema v0 on PostgreSQL 19: extensions, the RLS audit, and tenant isolation via real roles."""

from uuid import uuid7

import psycopg
import pytest
from sqlalchemy.exc import DBAPIError

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one
from scripts.audit_rls import find_violations
from tests.support.database import DisposableDatabase


def test_server_is_postgresql_19_with_required_extensions(test_db: DisposableDatabase) -> None:
    with psycopg.connect(test_db.superuser_url) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()
        exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
    assert version is not None
    assert int(version[0]) >= 190000
    assert {"vector", "pg_trgm", "pgcrypto", "pg_partman"} <= exts


def test_rls_audit_has_no_violations(test_db: DisposableDatabase) -> None:
    with psycopg.connect(test_db.migrator_url) as conn:
        assert find_violations(conn) == []


def test_rls_audit_detects_unprotected_tenant_table(test_db: DisposableDatabase) -> None:
    with psycopg.connect(test_db.migrator_url) as conn:
        conn.execute("CREATE TABLE audit_probe (id int PRIMARY KEY, org_id uuid NOT NULL)")
        try:
            problems = find_violations(conn)
        finally:
            conn.rollback()
    assert any(p.startswith("audit_probe: row level security") for p in problems)


async def _create_org(db: Database, slug: str) -> object:
    org_id = uuid7()
    async with db.transaction(TenantContext(org_id)) as conn:
        await conn.exec_driver_sql(
            "INSERT INTO orgs (id, name, slug) VALUES (%(id)s, %(name)s, %(slug)s)",
            {"id": org_id, "name": slug.title(), "slug": slug},
        )
    return org_id


async def test_app_role_sees_only_its_own_org(app_db: Database) -> None:
    a = await _create_org(app_db, f"org-a-{uuid7().hex[:8]}")
    b = await _create_org(app_db, f"org-b-{uuid7().hex[:8]}")
    async with app_db.transaction(TenantContext(a)) as conn:  # type: ignore[arg-type]
        rows = await fetch_all(conn, "SELECT id FROM orgs")
    assert [r["id"] for r in rows] == [a]
    async with app_db.transaction(TenantContext(b)) as conn:  # type: ignore[arg-type]
        assert await fetch_one(conn, "SELECT id FROM orgs WHERE id = :a", {"a": a}) is None


async def test_no_tenant_context_returns_no_rows(app_db: Database) -> None:
    await _create_org(app_db, f"org-c-{uuid7().hex[:8]}")
    async with app_db.transaction() as conn:
        assert await fetch_all(conn, "SELECT id FROM orgs") == []


async def test_app_cannot_write_rows_for_another_org(app_db: Database) -> None:
    mine, other = uuid7(), uuid7()
    with pytest.raises(DBAPIError, match="row-level security"):
        async with app_db.transaction(TenantContext(mine)) as conn:
            await conn.exec_driver_sql(
                "INSERT INTO orgs (id, name, slug) VALUES (%(id)s, 'x', %(slug)s)",
                {"id": other, "slug": f"x-{other.hex[:8]}"},
            )


async def test_worker_role_reads_across_orgs(app_db: Database, worker_db: Database) -> None:
    a = await _create_org(app_db, f"org-w-{uuid7().hex[:8]}")
    async with worker_db.transaction() as conn:
        assert await fetch_one(conn, "SELECT id FROM orgs WHERE id = :a", {"a": a}) is not None
