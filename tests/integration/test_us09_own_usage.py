"""US-09: a member sees their own usage and remaining budget.

Acceptance: the numbers match the ledger within one request, and a member cannot see other
members' data.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _setup(
    app: FastAPI, client: httpx2.AsyncClient, db: DisposableDatabase
) -> tuple[str, str, list[tuple[UUID, str]]]:
    """An org with one team and two members; returns (admin token, team id, [(id, token)])."""
    admin = await signup(client)
    token = admin["access_token"]
    org_id = UUID(admin["org"]["id"])
    team = (
        await client.post(
            "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
        )
    ).json()["id"]
    members = []
    for _ in range(2):
        user = add_user(db, org_id)
        added = await client.post(
            f"/v1/admin/teams/{team}/members", json={"user_id": str(user)}, headers=bearer(token)
        )
        assert added.status_code == 201
        members.append((user, app.state.container.tokens.issue_access(user, org_id)))
    return token, team, members


async def _chat(client: httpx2.AsyncClient, token: str, stream: bool = False) -> httpx2.Response:
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(token),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": "how much have I spent?"}],
            "max_tokens": 20,
            "stream": stream,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _usage(client: httpx2.AsyncClient, token: str) -> dict[str, Any]:
    resp = await client.get("/v1/me/usage", headers=bearer(token))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _ledger(db: DisposableDatabase, user: UUID, period: dict[str, Any]) -> tuple[int, ...]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT count(*), coalesce(sum(cost_micro_usd), 0), coalesce(sum(input_tokens), 0),"
            " coalesce(sum(output_tokens), 0) FROM usage_events WHERE user_id = %s"
            " AND ts >= (%s::date::timestamp AT TIME ZONE 'UTC')"
            " AND ts < (%s::date::timestamp AT TIME ZONE 'UTC')",
            (user, period["period_start"], period["period_end"]),
        ).fetchone()
    assert row is not None
    return tuple(int(v) for v in row)


async def test_own_usage_and_remaining_budget_match_the_ledger(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, [(me, my_token), _] = await _setup(app, client, test_db)
    budget = (
        await client.put(
            "/v1/admin/budgets",
            json={
                "scope_type": "member",
                "team_id": team,
                "user_id": str(me),
                "period": "month",
                "unit": "usd",
                "limit_amount": 1_000_000,
            },
            headers=bearer(admin),
        )
    ).json()

    empty = await _usage(client, my_token)
    assert [p["period"] for p in empty["usage"]] == ["day", "week", "month"]
    assert all(p["requests"] == 0 for p in empty["usage"])
    assert empty["timezone"] == "UTC"

    for _ in range(3):
        await _chat(client, my_token)
    await _chat(client, my_token, stream=True)
    await app.state.container.chat_service.drain()  # the stream settles in the background

    report = await _usage(client, my_token)
    for period in report["usage"]:
        ledger = _ledger(test_db, me, period)
        mine = (
            period["requests"],
            period["cost_micro_usd"],
            period["input_tokens"],
            period["output_tokens"],
        )
        assert mine == ledger
        assert ledger[0] == 4
    (mine_budget,) = report["budgets"]
    month = report["usage"][2]
    assert mine_budget["id"] == budget["id"]
    assert mine_budget["committed"] == month["cost_micro_usd"]  # counters equal the ledger
    assert mine_budget["reserved"] == 0
    assert mine_budget["remaining"] == 1_000_000 - month["cost_micro_usd"]

    await _chat(client, my_token)  # one more request shows up immediately
    after = await _usage(client, my_token)
    assert after["usage"][0]["requests"] == report["usage"][0]["requests"] + 1
    assert after["budgets"][0]["committed"] > mine_budget["committed"]


async def test_members_never_see_each_others_usage_or_budgets(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, [(me, my_token), (peer, peer_token)] = await _setup(app, client, test_db)
    for user in (me, peer):
        resp = await client.put(
            "/v1/admin/budgets",
            json={
                "scope_type": "member",
                "team_id": team,
                "user_id": str(user),
                "period": "day",
                "unit": "tokens",
                "limit_amount": 10_000,
            },
            headers=bearer(admin),
        )
        assert resp.status_code == 201
    await _chat(client, peer_token)
    await _chat(client, peer_token)

    mine = await _usage(client, my_token)
    assert mine["user_id"] == str(me)
    assert all(p["requests"] == 0 for p in mine["usage"])
    assert [b["committed"] for b in mine["budgets"]] == [0]
    theirs = await _usage(client, peer_token)
    assert theirs["usage"][0]["requests"] == 2
    assert {b["id"] for b in mine["budgets"]}.isdisjoint({b["id"] for b in theirs["budgets"]})
    assert (await client.get("/v1/admin/budgets", headers=bearer(my_token))).status_code == 403
    alerts = await client.get("/v1/admin/budget-alerts", headers=bearer(my_token))
    assert alerts.status_code == 403


async def test_api_keys_report_their_user_and_unbound_keys_are_refused(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, [(me, my_token), _] = await _setup(app, client, test_db)

    async def key(user: UUID | None) -> str:
        body: dict[str, Any] = {"name": f"k-{uuid4().hex[:4]}"}
        if user is not None:
            body["user_id"] = str(user)
        resp = await client.post(
            f"/v1/admin/teams/{team}/api-keys", json=body, headers=bearer(admin)
        )
        assert resp.status_code == 201, resp.text
        out: str = resp.json()["key"]
        return out

    bound, unbound = await key(me), await key(None)
    await _chat(client, bound)
    via_key = await _usage(client, bound)
    via_jwt = await _usage(client, my_token)
    assert via_key == via_jwt
    assert via_key["usage"][0]["requests"] == 1
    refused = await client.get("/v1/me/usage", headers=bearer(unbound))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "not_a_user_credential"
