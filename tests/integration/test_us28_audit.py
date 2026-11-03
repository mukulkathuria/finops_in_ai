"""US-28: audit trail.

Acceptance: logins, key changes, limit changes, policy changes and denials are recorded
append-only with a verifiable hash chain. (Login, key and limit events are added by US-03, US-04
and US-08 and asserted in their own suites; this suite proves the trail itself.)
"""

from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError

from gateway.adapters.postgres.database import Database, TenantContext
from tests.support.app import STRONG_PASSWORD, bearer, signup, unique_email
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _entries(client: httpx2.AsyncClient, token: str) -> list[dict[str, Any]]:
    resp = await client.get("/v1/admin/audit", headers=bearer(token))
    assert resp.status_code == 200, resp.text
    data: list[dict[str, Any]] = resp.json()["data"]
    return data


async def _verify(client: httpx2.AsyncClient, token: str) -> dict[str, Any]:
    resp = await client.get("/v1/admin/audit/verify", headers=bearer(token))
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()
    return result


def _superuser(db: DisposableDatabase, sql: str, *params: object) -> None:
    with psycopg.connect(db.superuser_url) as conn:
        conn.execute("SET session_replication_role = replica")  # bypass the immutability trigger
        conn.execute(sql, params)  # type: ignore[arg-type]


async def _org_with_denial(api: Api, test_db: DisposableDatabase) -> dict[str, Any]:
    app, client = api
    body = await signup(client)
    member = add_user(test_db, body["org"]["id"])
    member_token = app.state.container.tokens.issue_access(member, UUID(body["org"]["id"]))
    denied = await client.get("/v1/admin/audit", headers=bearer(member_token))
    assert denied.status_code == 403
    return {**body, "member_id": str(member)}


async def test_signup_is_audited_with_actor_target_ip_and_request_id(api: Api) -> None:
    _, client = api
    request_id = f"req-{uuid4().hex}"
    resp = await client.post(
        "/v1/auth/signup",
        json={"org_name": "Audited Co", "email": unique_email(), "password": STRONG_PASSWORD},
        headers={"X-Request-ID": request_id},
    )
    assert resp.status_code == 201
    assert resp.headers["x-request-id"] == request_id
    body = resp.json()
    [entry] = await _entries(client, body["access_token"])
    assert entry["seq"] == 1
    assert entry["action"] == "auth.signup"
    assert entry["actor_type"] == "user"
    assert entry["actor_id"] == body["user"]["id"]
    assert (entry["target_type"], entry["target_id"]) == ("org", body["org"]["id"])
    assert entry["request_id"] == request_id
    assert entry["ip"] == "127.0.0.1"
    assert entry["details"]["roles"] == ["org_admin", "member"]
    assert len(entry["hash"]) == 64


async def test_generated_request_ids_are_returned(api: Api) -> None:
    _, client = api
    resp = await client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert resp.headers["x-request-id"] != "bad id with spaces"
    assert len(resp.headers["x-request-id"]) == 36


async def test_access_denials_are_audited(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    org = await _org_with_denial(api, test_db)
    latest = (await _entries(client, org["access_token"]))[0]
    assert latest["action"] == "authz.access_denied"
    assert latest["actor_id"] == org["member_id"]
    assert latest["target_id"] == "GET /v1/admin/audit"
    assert latest["details"] == {"missing_permissions": ["audit:read"]}
    assert await _verify(client, org["access_token"]) == {
        "ok": True,
        "checked": 2,
        "first_bad_seq": None,
    }


async def test_an_edited_entry_breaks_the_chain(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    org = await _org_with_denial(api, test_db)
    _superuser(
        test_db,
        'UPDATE audit_log SET details = \'{"org_slug": "forged"}\' WHERE org_id = %s AND seq = 1',
        org["org"]["id"],
    )
    assert await _verify(client, org["access_token"]) == {
        "ok": False,
        "checked": 0,
        "first_bad_seq": 1,
    }


async def test_a_deleted_entry_breaks_the_chain(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    org = await _org_with_denial(api, test_db)
    _superuser(test_db, "DELETE FROM audit_log WHERE org_id = %s AND seq = 1", org["org"]["id"])
    result = await _verify(client, org["access_token"])
    assert result["ok"] is False
    assert result["first_bad_seq"] == 2


async def test_a_truncated_tail_is_detected(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    org = await _org_with_denial(api, test_db)
    _superuser(test_db, "DELETE FROM audit_log WHERE org_id = %s AND seq = 2", org["org"]["id"])
    assert await _verify(client, org["access_token"]) == {
        "ok": False,
        "checked": 1,
        "first_bad_seq": 2,
    }


async def test_app_role_cannot_rewrite_history(api: Api, app_db: Database) -> None:
    _, client = api
    body = await signup(client)
    ctx = TenantContext(UUID(body["org"]["id"]), UUID(body["user"]["id"]))
    for statement in ("UPDATE audit_log SET action = 'auth.nothing'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with app_db.transaction(ctx) as conn:
                await conn.exec_driver_sql(statement)


async def test_owner_sees_nothing_without_context_and_cannot_edit_with_it(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    body = await signup(client)
    with psycopg.connect(test_db.migrator_url) as conn:
        # FORCE ROW LEVEL SECURITY: even the table owner sees no rows without a tenant context.
        assert conn.execute("UPDATE audit_log SET actor_type = 'system'").rowcount == 0
        conn.execute("SELECT set_config('app.org_id', %s, false)", (body["org"]["id"],))
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("UPDATE audit_log SET actor_type = 'system'")


async def test_audit_trail_is_tenant_isolated(api: Api) -> None:
    _, client = api
    a, b = await signup(client), await signup(client)
    seen = {e["target_id"] for e in await _entries(client, a["access_token"])}
    assert a["org"]["id"] in seen
    assert b["org"]["id"] not in seen


def test_audit_log_is_partitioned_and_partitions_are_not_granted(
    test_db: DisposableDatabase,
) -> None:
    with psycopg.connect(test_db.superuser_url) as conn:
        parts = [
            r[0]
            for r in conn.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " WHERE i.inhparent = 'public.audit_log'::regclass"
            ).fetchall()
        ]
        granted = [
            p
            for p in parts
            for role in ("app", "worker", "readonly_analytics")
            if conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, p)).fetchone()
            == (True,)
        ]
    assert len(parts) >= 6  # current month, 4 premade, plus the default partition
    assert granted == []
