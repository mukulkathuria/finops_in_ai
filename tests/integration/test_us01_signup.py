"""US-01: an org admin signs up with local login.

Acceptance: valid details create an org and an admin and issue a JWT; weak passwords and duplicate
emails are rejected; passwords are Argon2id hashed.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI

from gateway.adapters.security.tokens import b64url_decode
from tests.support.app import (
    STRONG_PASSWORD,
    app_client,
    app_settings,
    bearer,
    signup,
    unique_email,
)
from tests.support.database import DisposableDatabase


@pytest.fixture(scope="session")
async def api(test_db: DisposableDatabase) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    async with app_client(app_settings(test_db)) as pair:
        yield pair


def _count_users(db: DisposableDatabase, email: str) -> int:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute("SELECT count(*) FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None
    return int(row[0])


async def test_signup_creates_org_and_admin_and_issues_jwt(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    app, client = api
    email = unique_email("founder")
    body = await signup(client, email=email.upper(), org_name="Acme Analytics")
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["org"]["slug"] == "acme-analytics" or body["org"]["slug"].startswith("acme")
    assert body["user"]["email"] == email
    assert set(body["user"]["roles"]) == {"org_admin", "member"}

    claims = app.state.container.tokens.verify_access(body["access_token"])
    assert str(claims.user_id) == body["user"]["id"]
    assert str(claims.org_id) == body["org"]["id"]

    me = await client.get("/v1/auth/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["roles"] == ["member", "org_admin"]

    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None
    assert row[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert STRONG_PASSWORD not in row[0]


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("Sh0rt!pw", "at least 12 characters"),
        ("password1234", "commonly used"),
        ("abababababab", "6 different characters"),
    ],
)
async def test_weak_passwords_are_rejected_and_nothing_is_created(
    api: tuple[FastAPI, httpx2.AsyncClient],
    test_db: DisposableDatabase,
    password: str,
    fragment: str,
) -> None:
    _, client = api
    email = unique_email("weak")
    resp = await client.post(
        "/v1/auth/signup", json={"org_name": "Weak Co", "email": email, "password": password}
    )
    assert resp.status_code == 422
    error: dict[str, Any] = resp.json()["error"]
    assert error["code"] == "weak_password"
    assert error["param"] == "password"
    assert any(fragment in p for p in error["problems"])
    assert _count_users(test_db, email) == 0


async def test_duplicate_email_is_rejected_case_insensitively(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    _, client = api
    email = unique_email("dup")
    await signup(client, email=email)
    resp = await client.post(
        "/v1/auth/signup",
        json={"org_name": "Second Org", "email": email.upper(), "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"
    assert _count_users(test_db, email) == 1


async def test_duplicate_slug_is_rejected(api: tuple[FastAPI, httpx2.AsyncClient]) -> None:
    _, client = api
    slug = f"slug-{unique_email().split('.')[1][:8]}"
    payload = {"org_name": "Slug One", "org_slug": slug, "password": STRONG_PASSWORD}
    first = await client.post("/v1/auth/signup", json={**payload, "email": unique_email()})
    second = await client.post("/v1/auth/signup", json={**payload, "email": unique_email()})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "slug_taken"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"org_name": "X", "email": "not-an-email", "password": STRONG_PASSWORD}, "invalid_email"),
        (
            {"org_name": "X", "email": "a@example.com", "password": STRONG_PASSWORD, "admin": True},
            "invalid_request",
        ),
        (
            {"org_name": "", "email": "a@example.com", "password": STRONG_PASSWORD},
            "invalid_request",
        ),
        (
            {
                "org_name": "X",
                "org_slug": "Bad Slug",
                "email": "a@example.com",
                "password": STRONG_PASSWORD,
            },
            "invalid_slug",
        ),
    ],
)
async def test_invalid_signup_payloads_are_rejected(
    api: tuple[FastAPI, httpx2.AsyncClient], payload: dict[str, Any], code: str
) -> None:
    _, client = api
    resp = await client.post("/v1/auth/signup", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


async def test_bootstrap_mode_allows_only_the_first_org(test_db: DisposableDatabase) -> None:
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute("UPDATE system_state SET value = 'false' WHERE key = 'bootstrap_completed'")
    async with app_client(app_settings(test_db, signup_mode="bootstrap")) as (_, client):
        await signup(client)
        resp = await client.post(
            "/v1/auth/signup",
            json={"org_name": "Late Org", "email": unique_email(), "password": STRONG_PASSWORD},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "signup_closed"


async def test_disabled_signup_is_forbidden(test_db: DisposableDatabase) -> None:
    async with app_client(app_settings(test_db, signup_mode="disabled")) as (_, client):
        resp = await client.post(
            "/v1/auth/signup",
            json={"org_name": "Nope", "email": unique_email(), "password": STRONG_PASSWORD},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "signup_disabled"


async def test_jwks_verifies_issued_tokens(api: tuple[FastAPI, httpx2.AsyncClient]) -> None:
    _, client = api
    body = await signup(client)
    keys = (await client.get("/.well-known/jwks.json")).json()["keys"]
    kid = jwt.get_unverified_header(body["access_token"])["kid"]
    jwk = next(k for k in keys if k["kid"] == kid)
    public = Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))
    claims = jwt.decode(body["access_token"], public, algorithms=["EdDSA"], audience="finops")
    assert claims["sub"] == body["user"]["id"]


async def test_protected_route_rejects_missing_tampered_and_foreign_tokens(
    api: tuple[FastAPI, httpx2.AsyncClient],
) -> None:
    _, client = api
    body = await signup(client)
    header, payload, sig = body["access_token"].split(".")
    foreign = jwt.encode(
        jwt.decode(body["access_token"], options={"verify_signature": False}),
        Ed25519PrivateKey.generate(),
        algorithm="EdDSA",
        headers={"kid": jwt.get_unverified_header(body["access_token"])["kid"]},
    )
    cases = {
        "missing_token": {},
        "invalid_token": bearer(f"{header}.{payload}x.{sig}"),
        "invalid_token ": bearer(foreign),
    }
    for expected, headers in cases.items():
        resp = await client.get("/v1/auth/me", headers=headers)
        assert resp.status_code == 401, expected
        assert resp.json()["error"]["code"] == expected.strip()


async def test_whitespace_org_name_is_rejected(api: tuple[FastAPI, httpx2.AsyncClient]) -> None:
    _, client = api
    resp = await client.post(
        "/v1/auth/signup",
        json={"org_name": "   ", "email": unique_email(), "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_org_name"


async def test_disabled_account_token_stops_working_immediately(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    _, client = api
    body = await signup(client)
    assert (
        await client.get("/v1/auth/me", headers=bearer(body["access_token"]))
    ).status_code == 200
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (body["user"]["id"],))
    resp = await client.get("/v1/auth/me", headers=bearer(body["access_token"]))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "account_inactive"
