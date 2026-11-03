"""US-06: org admins add provider keys and choose the adapter.

Acceptance: keys are stored encrypted and never returned by the API; switching a provider between
adapters is configuration only (the LiteLLM side fails closed until it can pass the contract
suite, ADR 0008).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from mock_llm.app import app as mock_provider
from starlette.types import Receive, Scope, Send

from gateway.app.main import create_app
from tests.support.app import app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

ORG_KEY = "org-own-provider-key-0123456789"


class Recorder:
    """Wraps the mock provider and remembers the Authorization header of each upstream call."""

    def __init__(self) -> None:
        self.auth: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            self.auth.append(headers.get(b"authorization", b"").decode())
        await mock_provider(scope, receive, send)


@pytest.fixture(scope="module")
async def rig(
    test_db: DisposableDatabase,
) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient, Recorder]]:
    recorder = Recorder()
    providers = httpx2.AsyncClient(
        mounts={"http://mock-llm:8000": httpx2.ASGITransport(app=recorder)}
    )
    app = create_app(
        app_settings(test_db), http_client=providers, environ={"MOCK_LLM_API_KEY": "operator-key"}
    )
    async with providers, app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://gateway.test"
        ) as client:
            yield app, client, recorder


async def _team_key(client: httpx2.AsyncClient, admin: dict[str, Any]) -> str:
    team = (
        await client.post(
            "/v1/admin/teams", json={"name": "Keys"}, headers=bearer(admin["access_token"])
        )
    ).json()["id"]
    return str(
        (
            await client.post(
                f"/v1/admin/teams/{team}/api-keys",
                json={"name": "k"},
                headers=bearer(admin["access_token"]),
            )
        ).json()["key"]
    )


async def _chat(client: httpx2.AsyncClient, key: str) -> httpx2.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "mock/mock-small", "messages": [{"role": "user", "content": "hi"}]},
    )


async def test_key_is_stored_encrypted_and_never_returned(
    rig: tuple[FastAPI, httpx2.AsyncClient, Recorder], test_db: DisposableDatabase
) -> None:
    _, client, _ = rig
    admin = await signup(client)
    resp = await client.put(
        "/v1/admin/providers/mock/key",
        json={"api_key": ORG_KEY, "label": "prod"},
        headers=bearer(admin["access_token"]),
    )
    assert resp.status_code == 201
    assert resp.json()["key_hint"] == "6789"
    assert ORG_KEY not in resp.text
    listed = await client.get("/v1/admin/providers", headers=bearer(admin["access_token"]))
    assert ORG_KEY not in listed.text
    assert [c["provider"] for c in listed.json()["data"]] == ["mock"]
    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute(
            "SELECT ciphertext, wrapped_key, key_id FROM provider_credentials WHERE org_id = %s",
            (admin["org"]["id"],),
        ).fetchone()
    assert row is not None
    assert ORG_KEY.encode() not in bytes(row[0]) + bytes(row[1])
    audit = (await client.get("/v1/admin/audit", headers=bearer(admin["access_token"]))).json()[
        "data"
    ]
    assert audit[0]["action"] == "provider_keys.added"
    assert ORG_KEY not in str(audit)


async def test_requests_use_the_org_key_and_others_use_the_operator_key(
    rig: tuple[FastAPI, httpx2.AsyncClient, Recorder],
) -> None:
    _, client, recorder = rig
    with_key, without_key = await signup(client), await signup(client)
    await client.put(
        "/v1/admin/providers/mock/key",
        json={"api_key": ORG_KEY},
        headers=bearer(with_key["access_token"]),
    )
    assert (await _chat(client, await _team_key(client, with_key))).status_code == 200
    assert recorder.auth[-1] == f"Bearer {ORG_KEY}"
    assert (await _chat(client, await _team_key(client, without_key))).status_code == 200
    assert recorder.auth[-1] == "Bearer operator-key"


async def test_rotation_revokes_the_previous_key(
    rig: tuple[FastAPI, httpx2.AsyncClient, Recorder],
) -> None:
    _, client, _ = rig
    admin = await signup(client)
    for key in (ORG_KEY, ORG_KEY + "-v2"):
        await client.put(
            "/v1/admin/providers/mock/key",
            json={"api_key": key},
            headers=bearer(admin["access_token"]),
        )
    listed = (
        await client.get("/v1/admin/providers", headers=bearer(admin["access_token"]))
    ).json()["data"]
    assert [c["key_hint"] for c in listed] == ["9-v2"]
    actions = [
        e["action"]
        for e in (
            await client.get("/v1/admin/audit", headers=bearer(admin["access_token"]))
        ).json()["data"]
    ]
    assert actions[0] == "provider_keys.rotated"


async def test_delete_and_validation(rig: tuple[FastAPI, httpx2.AsyncClient, Recorder]) -> None:
    _, client, _ = rig
    admin = await signup(client)
    token = admin["access_token"]
    assert (
        await client.put(
            "/v1/admin/providers/nope/key", json={"api_key": ORG_KEY}, headers=bearer(token)
        )
    ).status_code == 404
    assert (
        await client.put(
            "/v1/admin/providers/mock/key", json={"api_key": "short"}, headers=bearer(token)
        )
    ).status_code == 422
    await client.put(
        "/v1/admin/providers/mock/key", json={"api_key": ORG_KEY}, headers=bearer(token)
    )
    assert (
        await client.delete("/v1/admin/providers/mock/key", headers=bearer(token))
    ).status_code == 204
    assert (
        await client.delete("/v1/admin/providers/mock/key", headers=bearer(token))
    ).status_code == 404


async def test_only_admins_manage_provider_keys(
    rig: tuple[FastAPI, httpx2.AsyncClient, Recorder], test_db: DisposableDatabase
) -> None:
    app, client, _ = rig
    admin = await signup(client)
    org = UUID(admin["org"]["id"])
    member = app.state.container.tokens.issue_access(add_user(test_db, org), org)
    resp = await client.put(
        "/v1/admin/providers/mock/key", json={"api_key": ORG_KEY}, headers=bearer(member)
    )
    assert resp.status_code == 403
    assert (await client.get("/v1/admin/providers", headers=bearer(member))).status_code == 403
