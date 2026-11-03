"""US-08: team leads set member limits.

Acceptance: a lead sets and changes member limits inside their own team and under the team cap;
it fails for other teams or above the cap (unless overcommit allows the sum to exceed it); each
change is audited. Members ask for more budget and the lead approves it once or permanently.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]
DEMAND = 2 + 16 + 10  # the worst case a 10-token request for "hi" holds (see test_us07)


@dataclass(frozen=True)
class Org:
    admin: str
    team: str
    other_team: str
    lead: str
    lead_id: UUID
    m1: str
    m1_id: UUID
    m2: str
    m2_id: UUID
    outsider: str
    outsider_id: UUID


async def _team(client: httpx2.AsyncClient, token: str) -> str:
    resp = await client.post(
        "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
    )
    team: str = resp.json()["id"]
    return team


async def _org(app: FastAPI, client: httpx2.AsyncClient, db: DisposableDatabase) -> Org:
    admin = await signup(client)
    token = admin["access_token"]
    org_id = UUID(admin["org"]["id"])
    team, other = await _team(client, token), await _team(client, token)
    lead_id = add_user(db, org_id, roles=(("member", None), ("team_lead", UUID(team))))
    m1_id, m2_id, outsider_id = add_user(db, org_id), add_user(db, org_id), add_user(db, org_id)
    for user, into in ((lead_id, team), (m1_id, team), (m2_id, team), (outsider_id, other)):
        added = await client.post(
            f"/v1/admin/teams/{into}/members", json={"user_id": str(user)}, headers=bearer(token)
        )
        assert added.status_code == 201, added.text
    issue = app.state.container.tokens.issue_access
    return Org(
        admin=token,
        team=team,
        other_team=other,
        lead=issue(lead_id, org_id),
        lead_id=lead_id,
        m1=issue(m1_id, org_id),
        m1_id=m1_id,
        m2=issue(m2_id, org_id),
        m2_id=m2_id,
        outsider=issue(outsider_id, org_id),
        outsider_id=outsider_id,
    )


async def _put(
    client: httpx2.AsyncClient, token: str, scope: str, limit: int, **body: Any
) -> httpx2.Response:
    return await client.put(
        "/v1/admin/budgets",
        json={
            "scope_type": scope,
            "period": "day",
            "unit": "tokens",
            "limit_amount": limit,
            **body,
        },
        headers=bearer(token),
    )


async def _member(
    client: httpx2.AsyncClient, token: str, team: str, user: UUID, limit: int, **body: Any
) -> httpx2.Response:
    return await _put(client, token, "member", limit, team_id=team, user_id=str(user), **body)


async def _chat(client: httpx2.AsyncClient, token: str) -> httpx2.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=bearer(token),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )


def _audit(db: DisposableDatabase, target: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT action, details, actor_id FROM audit_log WHERE target_id = %s ORDER BY seq",
                (target,),
            ).fetchall()
        ]


async def test_lead_sets_and_changes_member_limits_inside_own_team(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    o = await _org(app, client, test_db)
    assert (await _put(client, o.admin, "team", 1000, team_id=o.team)).status_code == 201

    created = await _member(client, o.lead, o.team, o.m1_id, 400)
    assert created.status_code == 201, created.text
    changed = await _member(client, o.lead, o.team, o.m1_id, 500, reason="pairing week")
    assert changed.status_code == 200
    assert changed.json()["limit_amount"] == 500

    above = await _member(client, o.lead, o.team, o.m1_id, 1001)
    assert above.status_code == 422
    assert above.json()["error"]["code"] == "above_team_cap"
    other_team = await _member(client, o.lead, o.other_team, o.outsider_id, 10)
    assert other_team.status_code == 403
    not_in_team = await _member(client, o.lead, o.team, o.outsider_id, 10)
    assert not_in_team.status_code == 404
    assert not_in_team.json()["error"]["code"] == "member_not_found"
    own = await _member(client, o.lead, o.team, o.lead_id, 10)
    assert own.status_code == 403
    assert own.json()["error"]["code"] == "own_limit"
    assert (await _member(client, o.m1, o.team, o.m2_id, 10)).status_code == 403
    assert (await _put(client, o.lead, "team", 10, team_id=o.team)).status_code == 403
    by_admin = await _member(client, o.admin, o.team, o.lead_id, 2000)  # admins are not capped
    assert by_admin.status_code == 201

    trail = _audit(test_db, created.json()["id"])
    assert [(r[0], r[1]["old_limit"], r[1]["new_limit"]) for r in trail] == [
        ("budgets.limit_set", None, 400),
        ("budgets.limit_set", 400, 500),
    ]
    assert trail[1][1]["reason"] == "pairing week"
    assert {r[2] for r in trail} == {o.lead_id}

    mine = await client.get("/v1/admin/budgets", headers=bearer(o.lead))
    assert {(b["scope_type"], b["team_id"]) for b in mine.json()["data"]} == {
        ("team", o.team),
        ("member", o.team),
    }
    foreign = await client.get(
        "/v1/admin/budgets", params={"team_id": o.other_team}, headers=bearer(o.lead)
    )
    assert foreign.status_code == 403


async def test_overcommit_lets_member_limits_exceed_the_cap_and_strict_does_not(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    o = await _org(app, client, test_db)
    assert (await _put(client, o.admin, "team", 1000, team_id=o.team)).status_code == 201
    assert (await _member(client, o.lead, o.team, o.m1_id, 600)).status_code == 201
    assert (await _member(client, o.lead, o.team, o.m2_id, 600)).status_code == 201  # overcommit

    async def allocation(mode: str) -> httpx2.Response:
        return await client.put(
            "/v1/admin/budget-settings", json={"allocation": mode}, headers=bearer(o.admin)
        )

    refused = await allocation("strict")
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "allocation_exceeded"
    assert (await _member(client, o.lead, o.team, o.m2_id, 400)).status_code == 200
    assert (await allocation("strict")).json()["allocation"] == "strict"

    over = await _member(client, o.lead, o.team, o.m2_id, 401)
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "allocation_exceeded"
    assert (await _member(client, o.lead, o.team, o.m2_id, 400)).status_code == 200
    shrink = await _put(client, o.admin, "team", 999, team_id=o.team)
    assert shrink.status_code == 422
    assert (await _put(client, o.admin, "team", 1200, team_id=o.team)).status_code == 200
    assert (await _member(client, o.lead, o.team, o.m2_id, 600)).status_code == 200
    assert (await allocation("overcommit")).json()["allocation"] == "overcommit"


async def test_member_limits_bind_requests_before_the_team_cap(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    o = await _org(app, client, test_db)
    assert (await _put(client, o.admin, "team", 10**6, team_id=o.team)).status_code == 201
    limit = 2 * DEMAND + 5
    assert (await _member(client, o.lead, o.team, o.m1_id, limit)).status_code == 201
    for _ in range(10):
        resp = await _chat(client, o.m1)
        if resp.status_code == 429:
            break
        assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["scope"]["type"] == "member"
    assert error["scope"]["user_id"] == str(o.m1_id)
    assert (await _chat(client, o.m2)).status_code == 200  # no member limit of their own


async def test_more_budget_requests_are_approved_once_or_permanently_or_denied(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    o = await _org(app, client, test_db)
    assert (await _put(client, o.admin, "team", 10**6, team_id=o.team)).status_code == 201
    budget = (await _member(client, o.lead, o.team, o.m1_id, DEMAND - 1)).json()
    assert (await _chat(client, o.m1)).status_code == 429

    async def ask(token: str, budget_id: str, amount: int) -> httpx2.Response:
        return await client.post(
            "/v1/budget-requests",
            json={"budget_id": budget_id, "amount": amount, "reason": "customer demo"},
            headers=bearer(token),
        )

    async def decide(token: str, request_id: str, **body: Any) -> httpx2.Response:
        return await client.post(
            f"/v1/budget-requests/{request_id}/decision", json=body, headers=bearer(token)
        )

    first = await ask(o.m1, budget["id"], 100)
    assert first.status_code == 201, first.text
    assert (first.json()["status"], first.json()["unit"]) == ("pending", "tokens")
    rid = first.json()["id"]
    assert (await client.get("/v1/budget-requests", headers=bearer(o.m2))).json()["data"] == []
    queue = await client.get(
        "/v1/budget-requests", params={"status": "pending"}, headers=bearer(o.lead)
    )
    assert [r["id"] for r in queue.json()["data"]] == [rid]
    assert (await ask(o.outsider, budget["id"], 5)).status_code == 404  # not their budget
    assert (await decide(o.m2, rid, decision="approve", grant_type="one_time")).status_code == 403
    assert (await decide(o.lead, rid, decision="approve")).status_code == 422  # no grant type

    approved = await decide(o.lead, rid, decision="approve", grant_type="one_time")
    assert approved.status_code == 200
    assert (approved.json()["status"], approved.json()["grant_type"]) == ("approved", "one_time")
    assert approved.json()["decided_by"] == str(o.lead_id)
    again = await decide(o.lead, rid, decision="deny")
    assert again.status_code == 409
    assert (await _chat(client, o.m1)).status_code == 200  # the one-time grant applies now

    listed = await client.get(
        "/v1/admin/budgets", params={"team_id": o.team}, headers=bearer(o.lead)
    )
    (row,) = [b for b in listed.json()["data"] if b["id"] == budget["id"]]
    assert (row["limit_amount"], row["granted"]) == (DEMAND - 1, 100)

    permanent = (await ask(o.m1, budget["id"], 50)).json()["id"]
    raised = await decide(o.lead, permanent, decision="approve", grant_type="permanent")
    assert raised.status_code == 200
    trail = _audit(test_db, budget["id"])
    assert (trail[-1][1]["old_limit"], trail[-1][1]["new_limit"]) == (DEMAND - 1, DEMAND + 49)

    denied_id = (await ask(o.m1, budget["id"], 10**6)).json()["id"]
    denied = await decide(o.lead, denied_id, decision="deny", note="not this month")
    assert (denied.json()["status"], denied.json()["decision_note"]) == ("denied", "not this month")

    own_budget = (await _member(client, o.admin, o.team, o.lead_id, 10)).json()
    own = (await ask(o.lead, own_budget["id"], 10)).json()["id"]
    refused = await decide(o.lead, own, decision="approve", grant_type="one_time")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "own_request"
    assert (await decide(o.admin, own, decision="deny")).status_code == 200

    decided = _audit(test_db, rid)
    assert [r[0] for r in decided] == ["budget_requests.created", "budget_requests.decided"]
    assert decided[1][1]["grant_type"] == "one_time"
