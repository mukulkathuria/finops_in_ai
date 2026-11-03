"""US-03: members sign in and refresh their session.

Acceptance: the access token expires in about 15 minutes; refresh rotates; reuse of an old refresh
token revokes the whole token family. Also: lockout with backoff and per-address rate limits.
"""

import asyncio
import random
from typing import Any
from uuid import UUID

import httpx2
import psycopg
from argon2 import PasswordHasher
from fastapi import FastAPI

from gateway.adapters.postgres.counters import PostgresCounterStore
from gateway.adapters.postgres.database import Database
from tests.support.app import STRONG_PASSWORD, app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _login(
    client: httpx2.AsyncClient, email: str, password: str = STRONG_PASSWORD
) -> httpx2.Response:
    return await client.post("/v1/auth/login", json={"email": email, "password": password})


async def _refresh(client: httpx2.AsyncClient, token: str) -> httpx2.Response:
    return await client.post("/v1/auth/refresh", json={"refresh_token": token})


async def _audit_actions(client: httpx2.AsyncClient, admin_token: str) -> list[str]:
    resp = await client.get("/v1/admin/audit", headers=bearer(admin_token))
    return [e["action"] for e in resp.json()["data"]]


def _superuser(db: DisposableDatabase, sql: str, *params: object) -> None:
    with psycopg.connect(db.superuser_url) as conn:
        conn.execute(sql, params)  # type: ignore[arg-type]


async def test_login_returns_short_lived_access_and_a_refresh_token(api: Api) -> None:
    app, client = api
    admin = await signup(client)
    resp = await _login(client, admin["user"]["email"])
    assert resp.status_code == 200
    session: dict[str, Any] = resp.json()
    claims = app.state.container.tokens.verify_access(session["access_token"])
    assert claims.expires_at - claims.issued_at == 900
    assert session["refresh_token"].startswith("frt_")
    assert 0 < session["refresh_expires_in"] <= 14 * 86_400
    assert session["org"]["slug"] == admin["org"]["slug"]
    assert "auth.login_succeeded" in await _audit_actions(client, session["access_token"])


async def test_bad_password_unknown_and_malformed_emails_look_identical(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    answers = [
        await _login(client, admin["user"]["email"], "Wrong-Password-123"),
        await _login(client, "nobody.here@example.com"),
        await _login(client, "not-an-email"),
    ]
    assert {r.status_code for r in answers} == {401}
    assert {(r.json()["error"]["code"], r.json()["error"]["message"]) for r in answers} == {
        ("invalid_credentials", "email or password is incorrect")
    }


async def test_lockout_after_repeated_failures_then_backoff_expires(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin = await signup(client)
    email = admin["user"]["email"]
    for _ in range(5):
        assert (await _login(client, email, "Wrong-Password-123")).status_code == 401
    locked = await _login(client, email)  # even the right password waits out the lock, and
    # the answer is the one for a wrong password: a lock reveals no account (review finding M3)
    assert locked.status_code == 401
    assert locked.json()["error"]["code"] == "invalid_credentials"
    actions = await _audit_actions(client, admin["access_token"])
    assert actions.count("auth.login_failed") == 5
    assert "auth.login_locked" in actions

    _superuser(
        test_db,
        "UPDATE users SET locked_until = now() - interval '1 second' WHERE email = %s",
        email,
    )
    assert (await _login(client, email)).status_code == 200
    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute(
            "SELECT failed_logins, locked_until FROM users WHERE email = %s", (email,)
        ).fetchone()
    assert row == (0, None)


async def test_refresh_rotates_and_reuse_revokes_the_whole_family(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    first = (await _login(client, admin["user"]["email"])).json()
    rotated = await _refresh(client, first["refresh_token"])
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (
        await client.get("/v1/auth/me", headers=bearer(second["access_token"]))
    ).status_code == 200

    replay = await _refresh(client, first["refresh_token"])
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"
    newest = await _refresh(client, second["refresh_token"])
    assert newest.status_code == 401
    assert newest.json()["error"]["code"] == "refresh_token_revoked"
    assert "auth.token_family_revoked" in await _audit_actions(client, admin["access_token"])


async def test_concurrent_refreshes_of_one_token_have_one_winner_then_revoke(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    token = (await _login(client, admin["user"]["email"])).json()["refresh_token"]
    results = await asyncio.gather(*(_refresh(client, token) for _ in range(5)))
    winners = [r for r in results if r.status_code == 200]
    assert len(winners) == 1
    assert {r.status_code for r in results} == {200, 401}
    after = await _refresh(client, winners[0].json()["refresh_token"])
    assert after.status_code == 401


async def test_expired_refresh_token_is_rejected(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    admin = await signup(client)
    session = (await _login(client, admin["user"]["email"])).json()
    _superuser(
        test_db,
        "UPDATE refresh_tokens SET expires_at = now() - interval '1 second' WHERE user_id = %s",
        admin["user"]["id"],
    )
    resp = await _refresh(client, session["refresh_token"])
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "refresh_token_expired"


async def test_logout_revokes_the_family_and_is_idempotent(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    session = (await _login(client, admin["user"]["email"])).json()
    body = {"refresh_token": session["refresh_token"]}
    assert (await client.post("/v1/auth/logout", json=body)).status_code == 204
    assert (await _refresh(client, session["refresh_token"])).json()["error"][
        "code"
    ] == "refresh_token_revoked"
    assert (await client.post("/v1/auth/logout", json=body)).status_code == 204
    unknown = {"refresh_token": "frt_" + "x" * 40}
    assert (await client.post("/v1/auth/logout", json=unknown)).status_code == 204


async def test_disabled_users_cannot_log_in_or_refresh(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin = await signup(client)
    session = (await _login(client, admin["user"]["email"])).json()
    _superuser(test_db, "UPDATE users SET status = 'disabled' WHERE id = %s", admin["user"]["id"])
    assert (await _refresh(client, session["refresh_token"])).json()["error"][
        "code"
    ] == "account_inactive"
    assert (await _login(client, admin["user"]["email"])).json()["error"][
        "code"
    ] == "account_inactive"


async def test_unknown_refresh_token_is_rejected(api: Api) -> None:
    _, client = api
    resp = await _refresh(client, "frt_" + "y" * 43)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"


async def test_login_upgrades_weaker_password_hashes(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    admin = await signup(client)
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(STRONG_PASSWORD)
    _superuser(
        test_db, "UPDATE users SET password_hash = %s WHERE id = %s", weak, admin["user"]["id"]
    )
    assert (await _login(client, admin["user"]["email"])).status_code == 200
    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s", (admin["user"]["id"],)
        ).fetchone()
    assert row is not None
    assert row[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")


async def test_signup_also_returns_a_refresh_token_that_works(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    assert admin["refresh_token"].startswith("frt_")
    assert (await _refresh(client, admin["refresh_token"])).status_code == 200


async def test_public_auth_endpoints_are_rate_limited_per_address(
    test_db: DisposableDatabase,
) -> None:
    address = f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    settings = app_settings(test_db, auth_rate_limit_per_minute=3)
    async with app_client(settings, client_ip=address) as (_, client):
        statuses = [
            (await _login(client, "someone@example.com", "Wrong-Password-123")).status_code
            for _ in range(4)
        ]
        limited = await _login(client, "someone@example.com", "Wrong-Password-123")
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 60


async def test_counter_store_counts_per_key_within_a_window(app_db: Database) -> None:
    store = PostgresCounterStore(app_db)
    key = f"test:{UUID(int=random.getrandbits(128))}"
    decisions = [await store.hit(key, limit=2, window_s=3600) for _ in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert [d.hits for d in decisions] == [1, 2, 3]
    assert (await store.hit(key + ":other", limit=2, window_s=3600)).hits == 1
