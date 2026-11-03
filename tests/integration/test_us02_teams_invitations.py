"""US-02: org admins create teams and invite users.

Acceptance: an invited user can set a password via a single-use, expiring link; an expired or
reused link is rejected.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from tests.support.app import STRONG_PASSWORD, bearer, signup, unique_email
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _team(client: httpx2.AsyncClient, token: str, name: str | None = None) -> dict[str, Any]:
    resp = await client.post(
        "/v1/admin/teams", json={"name": name or f"Team {uuid4().hex[:6]}"}, headers=bearer(token)
    )
    assert resp.status_code == 201, resp.text
    team: dict[str, Any] = resp.json()
    return team


async def _invite(client: httpx2.AsyncClient, token: str, **payload: Any) -> httpx2.Response:
    body = {"email": unique_email("invitee"), **payload}
    return await client.post("/v1/admin/invitations", json=body, headers=bearer(token))


async def _accept(
    client: httpx2.AsyncClient, token: str, password: str = STRONG_PASSWORD
) -> httpx2.Response:
    return await client.post(
        "/v1/auth/invitations/accept",
        json={"token": token, "password": password, "display_name": "Ana"},
    )


async def test_admin_creates_team_and_invitee_joins_with_a_password(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    team = await _team(client, admin["access_token"], "Data Science")
    resp = await _invite(client, admin["access_token"], team_id=team["id"], team_role="member")
    assert resp.status_code == 201
    invitation = resp.json()
    assert invitation["invite_url"].endswith(f"/accept-invite#token={invitation['token']}")

    listed = await client.get("/v1/admin/invitations", headers=bearer(admin["access_token"]))
    assert [i["id"] for i in listed.json()["data"]] == [invitation["id"]]
    assert "token" not in listed.json()["data"][0]

    accepted = await _accept(client, invitation["token"])
    assert accepted.status_code == 201
    session = accepted.json()
    assert session["user"]["email"] == invitation["email"]
    assert session["user"]["roles"] == ["member"]
    assert session["org"]["id"] == admin["org"]["id"]
    assert session["org"]["slug"] == admin["org"]["slug"]

    me = await client.get("/v1/auth/me", headers=bearer(session["access_token"]))
    assert me.status_code == 200
    members = await client.get(
        f"/v1/admin/teams/{team['id']}/members", headers=bearer(session["access_token"])
    )
    assert [m["email"] for m in members.json()["data"]] == [invitation["email"]]


async def test_a_reused_link_is_rejected(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    invitation = (await _invite(client, admin["access_token"])).json()
    assert (await _accept(client, invitation["token"])).status_code == 201
    again = await _accept(client, invitation["token"])
    assert again.status_code == 410
    assert again.json()["error"]["code"] == "invitation_used"


async def test_an_expired_link_is_rejected(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    admin = await signup(client)
    invitation = (await _invite(client, admin["access_token"])).json()
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE invitations SET expires_at = now() - interval '1 minute' WHERE id = %s",
            (invitation["id"],),
        )
    resp = await _accept(client, invitation["token"])
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "invitation_expired"


async def test_revoked_and_superseded_links_are_rejected(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    first = (await _invite(client, admin["access_token"])).json()
    revoke = await client.delete(
        f"/v1/admin/invitations/{first['id']}", headers=bearer(admin["access_token"])
    )
    assert revoke.status_code == 204
    assert (await _accept(client, first["token"])).json()["error"]["code"] == "invitation_revoked"

    old = (await _invite(client, admin["access_token"], email="again@example.org")).json()
    new = (await _invite(client, admin["access_token"], email="again@example.org")).json()
    assert (await _accept(client, old["token"])).json()["error"]["code"] == "invitation_revoked"
    assert (await _accept(client, new["token"])).status_code == 201


async def test_unknown_or_malformed_tokens_are_rejected(api: Api) -> None:
    _, client = api
    resp = await _accept(client, "x" * 43)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "invitation_invalid"
    assert (await _accept(client, "short")).status_code == 422


async def test_weak_password_does_not_consume_the_link(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    invitation = (await _invite(client, admin["access_token"])).json()
    weak = await _accept(client, invitation["token"], password="password1234")
    assert weak.status_code == 422
    assert (await _accept(client, invitation["token"])).status_code == 201


async def test_concurrent_accepts_have_exactly_one_winner(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    invitation = (await _invite(client, admin["access_token"])).json()
    results = await asyncio.gather(*(_accept(client, invitation["token"]) for _ in range(8)))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(201) == 1
    assert set(statuses) <= {201, 410}


async def test_member_cannot_create_teams_or_invite_admins(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    member_token = app.state.container.tokens.issue_access(add_user(test_db, org_id), org_id)
    assert (
        await client.post("/v1/admin/teams", json={"name": "X"}, headers=bearer(member_token))
    ).status_code == 403
    resp = await _invite(client, member_token, org_role="org_admin")
    assert resp.status_code == 403


async def test_team_lead_invites_only_plain_members_into_their_own_team(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    mine = await _team(client, admin["access_token"])
    other = await _team(client, admin["access_token"])
    lead_id = add_user(test_db, org_id, roles=(("member", None), ("team_lead", UUID(mine["id"]))))
    lead = app.state.container.tokens.issue_access(lead_id, org_id)

    ok = await _invite(client, lead, team_id=mine["id"], team_role="member")
    assert ok.status_code == 201
    assert (await _invite(client, lead, team_id=other["id"], team_role="member")).status_code == 403
    assert (await _invite(client, lead, team_id=mine["id"], team_role="lead")).status_code == 403
    assert (await _invite(client, lead)).status_code == 403  # org-wide invitation needs an admin

    visible = await client.get("/v1/admin/invitations", headers=bearer(lead))
    assert [i["team_id"] for i in visible.json()["data"]] == [mine["id"]]


async def test_lead_manages_own_team_members_only(api: Api, test_db: DisposableDatabase) -> None:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    mine, other = (
        await _team(client, admin["access_token"]),
        await _team(client, admin["access_token"]),
    )
    lead_id = add_user(test_db, org_id, roles=(("member", None), ("team_lead", UUID(mine["id"]))))
    lead = app.state.container.tokens.issue_access(lead_id, org_id)
    newbie = add_user(test_db, org_id)

    add = await client.post(
        f"/v1/admin/teams/{mine['id']}/members", json={"user_id": str(newbie)}, headers=bearer(lead)
    )
    assert add.status_code == 201
    again = await client.post(
        f"/v1/admin/teams/{mine['id']}/members", json={"user_id": str(newbie)}, headers=bearer(lead)
    )
    assert again.status_code == 200
    foreign = await client.post(
        f"/v1/admin/teams/{other['id']}/members",
        json={"user_id": str(newbie)},
        headers=bearer(lead),
    )
    assert foreign.status_code == 403
    promote = await client.post(
        f"/v1/admin/teams/{mine['id']}/members",
        json={"user_id": str(newbie), "role": "lead"},
        headers=bearer(lead),
    )
    assert promote.status_code == 403  # only org admins appoint leads
    removed = await client.delete(
        f"/v1/admin/teams/{mine['id']}/members/{newbie}", headers=bearer(lead)
    )
    assert removed.status_code == 204
    gone = await client.delete(
        f"/v1/admin/teams/{mine['id']}/members/{newbie}", headers=bearer(lead)
    )
    assert gone.status_code == 404


async def test_admin_appoints_a_lead_who_gains_team_scoped_powers(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    team = await _team(client, admin["access_token"])
    user = add_user(test_db, org_id)
    resp = await client.post(
        f"/v1/admin/teams/{team['id']}/members",
        json={"user_id": str(user), "role": "lead"},
        headers=bearer(admin["access_token"]),
    )
    assert resp.status_code == 201
    token = app.state.container.tokens.issue_access(user, org_id)
    members = await client.get(f"/v1/admin/teams/{team['id']}/members", headers=bearer(token))
    assert members.json()["data"][0]["is_lead"] is True


async def test_teams_and_users_of_other_orgs_are_invisible(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    a, b = await signup(client), await signup(client)
    team_b = await _team(client, b["access_token"])
    user_b = add_user(test_db, b["org"]["id"])
    team_a = await _team(client, a["access_token"])
    cross_team = await client.post(
        f"/v1/admin/teams/{team_b['id']}/members",
        json={"user_id": a["user"]["id"]},
        headers=bearer(a["access_token"]),
    )
    assert cross_team.status_code == 404
    cross_user = await client.post(
        f"/v1/admin/teams/{team_a['id']}/members",
        json={"user_id": str(user_b)},
        headers=bearer(a["access_token"]),
    )
    assert cross_user.status_code == 404
    listed = await client.get("/v1/admin/teams", headers=bearer(a["access_token"]))
    assert team_b["id"] not in {t["id"] for t in listed.json()["data"]}


async def test_team_names_are_unique_per_org_case_insensitively(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    await _team(client, admin["access_token"], "Platform")
    dup = await client.post(
        "/v1/admin/teams", json={"name": "  platform "}, headers=bearer(admin["access_token"])
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "team_name_taken"


async def test_inviting_an_existing_member_is_a_conflict(api: Api) -> None:
    _, client = api
    admin = await signup(client)
    resp = await _invite(client, admin["access_token"], email=admin["user"]["email"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"
