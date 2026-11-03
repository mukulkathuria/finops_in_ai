"""US-04: team leads create team API keys.

Acceptance: the key is shown once, stored only as a hash, scoped to the team and revocable; a
revoked key fails within 5 seconds (here: on the very next request).
"""

import hashlib
import time
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _team(client: httpx2.AsyncClient, token: str) -> str:
    resp = await client.post(
        "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _create_key(
    client: httpx2.AsyncClient, token: str, team: str, **extra: Any
) -> httpx2.Response:
    return await client.post(
        f"/v1/admin/teams/{team}/api-keys", json={"name": "ci key", **extra}, headers=bearer(token)
    )


def _lead(app: FastAPI, db: DisposableDatabase, org_id: str, team: str) -> tuple[UUID, str]:
    user = add_user(db, org_id, roles=(("member", None), ("team_lead", UUID(team))))
    with psycopg.connect(db.superuser_url) as conn:
        conn.execute(
            "INSERT INTO team_members (org_id, team_id, user_id) VALUES (%s, %s, %s)",
            (org_id, team, user),
        )
    return user, app.state.container.tokens.issue_access(user, UUID(org_id))


async def test_lead_creates_a_key_shown_once_and_stored_only_as_a_hash(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    _, lead = _lead(app, test_db, admin["org"]["id"], team)

    resp = await _create_key(client, lead, team)
    assert resp.status_code == 201
    created = resp.json()
    key: str = created["key"]
    assert key.startswith(f"fo_{created['prefix']}_")
    assert created["team_id"] == team
    assert created["scopes"] == ["models:use"]

    listed = (await client.get(f"/v1/admin/teams/{team}/api-keys", headers=bearer(lead))).json()[
        "data"
    ]
    assert [k["prefix"] for k in listed] == [created["prefix"]]
    assert "key" not in listed[0]

    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE id = %s", (created["id"],)).fetchone()
    assert row is not None
    assert hashlib.sha256(key.encode()).hexdigest() in row
    assert all(key not in str(value) for value in row)


async def test_key_authenticates_to_its_team_and_revocation_is_immediate(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    created = (await _create_key(client, admin["access_token"], team)).json()
    who = await client.get("/v1/whoami", headers=bearer(created["key"]))
    assert who.status_code == 200
    assert who.json() | {"permissions": None} == {
        "org_id": admin["org"]["id"],
        "team_id": team,
        "user_id": None,
        "api_key_id": created["id"],
        "auth_method": "api_key",
        "permissions": None,
    }

    revoked_at = time.monotonic()
    resp = await client.delete(
        f"/v1/admin/teams/{team}/api-keys/{created['id']}", headers=bearer(admin["access_token"])
    )
    assert resp.status_code == 204
    after = await client.get("/v1/whoami", headers=bearer(created["key"]))
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "invalid_api_key"
    assert time.monotonic() - revoked_at < 5

    again = await client.delete(
        f"/v1/admin/teams/{team}/api-keys/{created['id']}", headers=bearer(admin["access_token"])
    )
    assert again.status_code == 410


async def test_keys_are_managed_only_by_the_team_lead_or_an_admin(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    mine, other = (
        await _team(client, admin["access_token"]),
        await _team(client, admin["access_token"]),
    )
    _, lead = _lead(app, test_db, admin["org"]["id"], mine)
    member = app.state.container.tokens.issue_access(
        add_user(test_db, admin["org"]["id"]), UUID(admin["org"]["id"])
    )
    assert (await _create_key(client, lead, other)).status_code == 403
    assert (
        await client.get(f"/v1/admin/teams/{other}/api-keys", headers=bearer(lead))
    ).status_code == 403
    assert (await _create_key(client, member, mine)).status_code == 403
    victim = (await _create_key(client, admin["access_token"], other)).json()
    stolen = await client.delete(
        f"/v1/admin/teams/{mine}/api-keys/{victim['id']}", headers=bearer(lead)
    )
    assert stolen.status_code == 404  # a key of another team is not visible through this team


async def test_keys_of_another_org_are_invisible(api: Api) -> None:
    _, client = api
    a, b = await signup(client), await signup(client)
    team_a = await _team(client, a["access_token"])
    key_a = (await _create_key(client, a["access_token"], team_a)).json()
    resp = await client.delete(
        f"/v1/admin/teams/{team_a}/api-keys/{key_a['id']}", headers=bearer(b["access_token"])
    )
    assert resp.status_code == 404
    assert (await client.get("/v1/whoami", headers=bearer(key_a["key"]))).status_code == 200


async def test_expired_and_malformed_keys_are_rejected(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    created = (await _create_key(client, admin["access_token"], team, expires_in_days=1)).json()
    assert created["expires_at"] is not None
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = %s",
            (created["id"],),
        )
    assert (await client.get("/v1/whoami", headers=bearer(created["key"]))).status_code == 401
    for bad in ("fo_short", "fo_ABCDEFGH_" + "x" * 10, created["key"][:-1] + "!"):
        resp = await client.get("/v1/whoami", headers=bearer(bad))
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"


async def test_member_bound_key_stops_working_when_the_member_leaves(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    member_id, _ = _lead(app, test_db, admin["org"]["id"], team)
    created = (
        await _create_key(client, admin["access_token"], team, user_id=str(member_id))
    ).json()
    assert (await client.get("/v1/whoami", headers=bearer(created["key"]))).json()[
        "user_id"
    ] == str(member_id)
    removed = await client.delete(
        f"/v1/admin/teams/{team}/members/{member_id}", headers=bearer(admin["access_token"])
    )
    assert removed.status_code == 204
    assert (await client.get("/v1/whoami", headers=bearer(created["key"]))).status_code == 401


async def test_key_cannot_be_bound_to_a_non_member(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    outsider = add_user(test_db, admin["org"]["id"])
    resp = await _create_key(client, admin["access_token"], team, user_id=str(outsider))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_key_owner"


async def test_key_lifecycle_is_audited(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    created = (await _create_key(client, admin["access_token"], team)).json()
    await client.delete(
        f"/v1/admin/teams/{team}/api-keys/{created['id']}", headers=bearer(admin["access_token"])
    )
    entries = (await client.get("/v1/admin/audit", headers=bearer(admin["access_token"]))).json()[
        "data"
    ]
    actions = [(e["action"], e["target_id"]) for e in entries]
    assert ("api_keys.created", created["id"]) in actions
    assert ("api_keys.revoked", created["id"]) in actions
    assert all(created["key"] not in str(e) for e in entries)


async def test_jwt_callers_pick_a_team_they_belong_to(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"])
    member_id, token = _lead(app, test_db, admin["org"]["id"], team)
    who = (await client.get("/v1/whoami", headers=bearer(token))).json()
    assert (who["auth_method"], who["team_id"], who["user_id"]) == ("jwt", team, str(member_id))
    other = await _team(client, admin["access_token"])
    denied = await client.get("/v1/whoami", headers={**bearer(token), "X-FinOps-Team": other})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "not_a_team_member"
