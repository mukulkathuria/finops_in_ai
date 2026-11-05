"""US-27: OIDC login maps to an existing user via `identities`, and local login can be disabled
per org (admins keep it). Through the gateway's own endpoints, against a fake provider
(tests/support/fake_idp.py) on PostgreSQL 19."""

from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx2
import psycopg
from argon2 import PasswordHasher
from fastapi import FastAPI

from tests.support import fake_idp
from tests.support.app import STRONG_PASSWORD, bearer, signup, unique_email
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _configure(
    client: httpx2.AsyncClient, admin: dict[str, str], **changes: Any
) -> httpx2.Response:
    body = {
        "issuer": fake_idp.ISSUER,
        "client_id": fake_idp.CLIENT_ID,
        "client_secret": fake_idp.CLIENT_SECRET,
        "allowed_domains": ["example.com"],
    } | changes
    return await client.put("/v1/admin/sso", json=body, headers=admin)


async def _to_provider(client: httpx2.AsyncClient, slug: str) -> dict[str, str]:
    started = await client.get("/v1/auth/sso/start", params={"org": slug})
    assert started.status_code == 303, started.text
    assert started.headers["cache-control"] == "no-store"
    location = urlsplit(started.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{fake_idp.ISSUER}/authorize"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _back(client: httpx2.AsyncClient, params: dict[str, str]) -> dict[str, str]:
    back = await client.get("/v1/auth/sso/callback", params=params)
    assert back.status_code == 303, back.text
    location = urlsplit(back.headers["location"])
    assert location.path == "/login/sso"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _sign_in(
    client: httpx2.AsyncClient, slug: str, *, subject: str, **arranged: Any
) -> dict[str, str]:
    request = await _to_provider(client, slug)
    code = f"code-{uuid4().hex}"
    arranged = {"nonce": request["nonce"]} | arranged
    fake_idp.arrange(
        code,
        challenge=request["code_challenge"],
        redirect_uri=request["redirect_uri"],
        subject=subject,
        **arranged,
    )
    return await _back(client, {"code": code, "state": request["state"]})


async def test_us27_a_provider_signs_in_an_existing_member_through_identities(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    owner = await signup(client)
    admin, slug, email = bearer(owner["access_token"]), owner["org"]["slug"], owner["user"]["email"]
    configured = await _configure(client, admin)
    assert configured.status_code == 200, configured.text
    assert configured.json()["confidential_client"] is True
    assert "client_secret" not in configured.json()  # it goes in and never comes out
    assert configured.json()["callback_url"] == "http://gateway.test/v1/auth/sso/callback"

    first = await _sign_in(client, slug, subject="idp-user-1", email=email.upper())
    session = await client.post("/v1/auth/sso/exchange", json={"code": first["code"]})
    assert session.status_code == 200, session.text
    assert session.json()["user"]["id"] == owner["user"]["id"]
    replayed = await client.post("/v1/auth/sso/exchange", json={"code": first["code"]})
    assert replayed.status_code == 401  # the one-time code is spent

    # The identity is stored: the next sign-in maps by subject, without any email.
    assert "code" in await _sign_in(client, slug, subject="idp-user-1")
    with psycopg.connect(test_db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT provider, subject, user_id FROM identities WHERE org_id = %s",
            (owner["org"]["id"],),
        ).fetchall()
    assert rows == [(fake_idp.ISSUER, "idp-user-1", UUID(owner["user"]["id"]))]

    # No account is ever created; an unverified email, or one outside the allowed domains,
    # links nothing.
    refused = {"error": "sso_no_account"}
    assert await _sign_in(client, slug, subject="idp-2", email="nobody@example.com") == refused
    unverified = await _sign_in(client, slug, subject="idp-3", email=email, email_verified=False)
    assert unverified == refused
    assert (await _configure(client, admin, allowed_domains=["corp.example"])).status_code == 200
    assert await _sign_in(client, slug, subject="idp-4", email=email) == refused


async def test_us27_forged_replayed_or_cancelled_sign_ins_are_refused(api: Api) -> None:
    _, client = api
    owner = await signup(client)
    admin, slug, email = bearer(owner["access_token"]), owner["org"]["slug"], owner["user"]["email"]
    assert (await _configure(client, admin)).status_code == 200

    request = await _to_provider(client, slug)
    code = f"code-{uuid4().hex}"
    fake_idp.arrange(
        code,
        nonce="not-the-nonce",
        challenge=request["code_challenge"],
        redirect_uri=request["redirect_uri"],
        subject="idp-user",
        email=email,
    )
    params = {"code": code, "state": request["state"]}
    assert await _back(client, params) == {"error": "sso_failed"}
    assert await _back(client, params) == {"error": "sso_expired"}  # the state is spent

    for forged in (
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"key": fake_idp.OTHER_KEY},
        {"exp": 1_000_000_000},
    ):
        failed = await _sign_in(client, slug, subject="idp-user", email=email, **forged)
        assert failed == {"error": "sso_failed"}, forged

    cancelled = await _to_provider(client, slug)
    denied = {"state": cancelled["state"], "error": "access_denied"}
    assert await _back(client, denied) == {"error": "sso_cancelled"}
    assert await _back(client, {"code": "x", "state": "never-issued"}) == {"error": "sso_expired"}

    unknown = await client.post("/v1/auth/sso/exchange", json={"code": "c" * 43})
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "invalid_sso_code"
    nowhere = await client.get("/v1/auth/sso/start", params={"org": "no-such-org"})
    assert nowhere.status_code == 404


async def test_us27_local_login_can_be_disabled_per_org(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    owner = await signup(client)
    admin, org_id = bearer(owner["access_token"]), owner["org"]["id"]
    member_email = unique_email("member")
    member = add_user(test_db, org_id, email=member_email)
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (PasswordHasher().hash(STRONG_PASSWORD), member),
        )
    login = {"email": member_email, "password": STRONG_PASSWORD}
    assert (await client.post("/v1/auth/login", json=login)).status_code == 200

    assert (await _configure(client, admin, local_login_disabled=True)).status_code == 200
    refused = await client.post("/v1/auth/login", json=login)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "sso_required"
    wrong = await client.post("/v1/auth/login", json={**login, "password": "Wrong-Horse-1!"})
    assert wrong.status_code == 401  # a wrong password learns nothing about single sign-on
    owner_login = {"email": owner["user"]["email"], "password": STRONG_PASSWORD}
    assert (await client.post("/v1/auth/login", json=owner_login)).status_code == 200  # admins
    other = await signup(client)  # another org is not affected
    other_login = {"email": other["user"]["email"], "password": STRONG_PASSWORD}
    assert (await client.post("/v1/auth/login", json=other_login)).status_code == 200

    member_token = app.state.container.tokens.issue_access(member, UUID(org_id))
    assert (await client.get("/v1/admin/sso", headers=bearer(member_token))).status_code == 403
    assert (await client.delete("/v1/admin/sso", headers=admin)).status_code == 204
    assert (await client.get("/v1/admin/sso", headers=admin)).status_code == 404
    assert (await client.post("/v1/auth/login", json=login)).status_code == 200  # local again


async def test_us27_a_sign_in_completes_only_in_the_browser_that_started_it(api: Api) -> None:
    """Login CSRF: someone signs in at the provider as themselves, then gets a victim's browser
    to finish the sign-in (the callback) or to trade their one-time code. Either would sign the
    victim into the attacker's account."""
    app, client = api
    owner = await signup(client)
    admin, slug, email = bearer(owner["access_token"]), owner["org"]["slug"], owner["user"]["email"]
    assert (await _configure(client, admin)).status_code == 200
    request = await _to_provider(client, slug)
    code = f"code-{uuid4().hex}"
    fake_idp.arrange(
        code,
        challenge=request["code_challenge"],
        redirect_uri=request["redirect_uri"],
        subject="idp-csrf",
        email=email,
        nonce=request["nonce"],
    )
    callback = {"code": code, "state": request["state"]}
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gateway.test") as victim:
        assert await _back(victim, callback) == {"error": "sso_expired"}  # another browser
        issued = await _back(client, callback)  # the browser that started it
        traded = await victim.post("/v1/auth/sso/exchange", json={"code": issued["code"]})
        assert traded.status_code == 401
    session = await client.post("/v1/auth/sso/exchange", json={"code": issued["code"]})
    assert session.status_code == 200, session.text
    assert session.json()["user"]["id"] == owner["user"]["id"]
