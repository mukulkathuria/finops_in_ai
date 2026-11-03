"""US-07: org and team budgets.

Acceptance: requests beyond a hard limit get `429 budget_exceeded` naming the scope; the soft
threshold sends one alert. Holds are released on failure, a rejection never moves a counter and
the counters always equal the ledger.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx2
import psycopg
import pytest
from fastapi import FastAPI

from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]
# The worst case a 10-token request for "hi" holds: prompt bytes + chat-template allowance + output.
DEMAND = 2 + 16 + 10


async def _org(client: httpx2.AsyncClient) -> tuple[dict[str, Any], str, str]:
    admin = await signup(client)
    token = admin["access_token"]
    team = (
        await client.post(
            "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
        )
    ).json()["id"]
    key = (
        await client.post(
            f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=bearer(token)
        )
    ).json()["key"]
    return admin, team, key


async def _set(client: httpx2.AsyncClient, token: str, **body: Any) -> dict[str, Any]:
    resp = await client.put(
        "/v1/admin/budgets",
        json={"period": "day", "unit": "tokens", **body},
        headers=bearer(token),
    )
    assert resp.status_code in (200, 201), resp.text
    out: dict[str, Any] = resp.json()
    return out


async def _chat(
    client: httpx2.AsyncClient, key: str, content: str = "hi", *, stream: bool = False
) -> httpx2.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 10,
            "stream": stream,
        },
    )


def _counters(db: DisposableDatabase, budget_id: str) -> tuple[int, int]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(committed), 0), coalesce(sum(reserved), 0)"
            " FROM budget_usage WHERE budget_id = %s",
            (budget_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _ledger(db: DisposableDatabase, team: str) -> tuple[int, int]:
    """(tokens, micro-USD) the ledger holds for one team."""
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(input_tokens + output_tokens), 0),"
            " coalesce(sum(cost_micro_usd), 0) FROM usage_events WHERE team_id = %s",
            (team,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


async def test_requests_beyond_a_team_limit_get_429_naming_the_team(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    limit = 2 * DEMAND + 5
    budget = await _set(
        client, admin["access_token"], scope_type="team", team_id=team, limit_amount=limit
    )
    committed = 0
    for _ in range(20):
        resp = await _chat(client, key)
        if resp.status_code == 429:
            break
        assert resp.status_code == 200, resp.text
        assert committed + DEMAND <= limit  # admitted only while the worst case still fitted
        committed += resp.json()["usage"]["total_tokens"]
    else:
        pytest.fail("the budget never ran out")
    assert committed + DEMAND > limit
    error = resp.json()["error"]
    assert (error["code"], error["type"]) == ("budget_exceeded", "rate_limit_error")
    assert error["scope"] == {
        "type": "team",
        "budget_id": budget["id"],
        "team_id": team,
        "user_id": None,
        "period": "day",
        "unit": "tokens",
    }
    assert error["request_more_url"] == "/v1/budget-requests"
    assert _counters(test_db, budget["id"]) == (committed, 0)  # the rejection moved nothing
    assert _ledger(test_db, team)[0] == committed


async def test_the_tightest_scope_is_named_team_before_org(api: Api) -> None:
    _, client = api
    admin, team, key = await _org(client)
    token = admin["access_token"]
    await _set(client, token, scope_type="org", unit="usd", limit_amount=1)
    rejected = await _chat(client, key)
    assert rejected.status_code == 429
    assert rejected.json()["error"]["scope"]["type"] == "org"
    await _set(client, token, scope_type="team", team_id=team, unit="usd", limit_amount=1)
    assert (await _chat(client, key)).json()["error"]["scope"]["type"] == "team"
    await _set(client, token, scope_type="org", unit="usd", limit_amount=10**9)
    await _set(client, token, scope_type="team", team_id=team, unit="usd", limit_amount=10**9)
    assert (await _chat(client, key)).status_code == 200


async def test_the_soft_threshold_sends_exactly_one_alert(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, _, key = await _org(client)
    token = admin["access_token"]
    budget = await _set(
        client, token, scope_type="org", period="month", limit_amount=1000, soft_pct=5
    )
    for _ in range(8):
        assert (await _chat(client, key)).status_code == 200
    committed, _ = _counters(test_db, budget["id"])
    assert committed >= 50
    alerts = (await client.get("/v1/admin/budget-alerts", headers=bearer(token))).json()["data"]
    assert len(alerts) == 1
    (alert,) = alerts
    assert (alert["budget_id"], alert["kind"], alert["scope_type"]) == (
        budget["id"],
        "soft_threshold",
        "org",
    )
    assert 50 <= alert["committed"] <= committed
    assert alert["limit_amount"] == 1000


async def test_failures_release_holds_and_streams_and_embeddings_settle(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, key = await _org(client)
    token = admin["access_token"]
    tokens = await _set(client, token, scope_type="team", team_id=team, limit_amount=10**6)
    money = await _set(
        client, token, scope_type="team", team_id=team, unit="usd", limit_amount=10**6
    )
    failed = await _chat(client, key, "[[mock:server_error]]")
    assert failed.status_code == 503
    assert _counters(test_db, tokens["id"]) == (0, 0)

    streamed = await _chat(client, key, stream=True)
    assert streamed.status_code == 200
    assert streamed.text.endswith("data: [DONE]\n\n")
    embedded = await client.post(
        "/v1/embeddings", headers=bearer(key), json={"model": "mock/mock-embed", "input": "abc"}
    )
    assert embedded.status_code == 200
    await app.state.container.chat_service.drain()  # streams settle in the background

    ledger_tokens, ledger_cost = _ledger(test_db, team)
    assert ledger_tokens > 0
    assert ledger_cost > 0
    assert _counters(test_db, tokens["id"]) == (ledger_tokens, 0)
    assert _counters(test_db, money["id"]) == (ledger_cost, 0)
    with psycopg.connect(test_db.superuser_url) as conn:
        holds = conn.execute(
            "SELECT status, count(*) FROM budget_reservations WHERE budget_id = %s"
            " GROUP BY status ORDER BY status",
            (tokens["id"],),
        ).fetchall()
    assert holds == [("committed", 2), ("released", 1)]  # stream + embedding; the failure


async def test_a_stream_over_budget_fails_before_the_first_byte(api: Api) -> None:
    _, client = api
    admin, team, key = await _org(client)
    await _set(
        client, admin["access_token"], scope_type="team", team_id=team, unit="usd", limit_amount=1
    )
    resp = await _chat(client, key, stream=True)
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "budget_exceeded"


async def test_a_limit_below_current_usage_blocks_without_touching_counters(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    token = admin["access_token"]
    budget = await _set(client, token, scope_type="team", team_id=team, limit_amount=10_000)
    for _ in range(3):
        assert (await _chat(client, key)).status_code == 200
    before = _counters(test_db, budget["id"])
    lowered = await _set(client, token, scope_type="team", team_id=team, limit_amount=1)
    assert lowered["id"] == budget["id"]
    assert lowered["remaining"] == 0
    assert lowered["committed"] == before[0]
    assert (await _chat(client, key)).status_code == 429
    assert _counters(test_db, budget["id"]) == before


async def test_only_org_admins_set_org_and_team_budgets(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, _ = await _org(client)
    token = admin["access_token"]
    org_id = UUID(admin["org"]["id"])
    tokens = app.state.container.tokens
    member = tokens.issue_access(add_user(test_db, org_id), org_id)
    lead = tokens.issue_access(
        add_user(test_db, org_id, roles=(("member", None), ("team_lead", UUID(team)))), org_id
    )
    for who in (member, lead):
        for body in (
            {"scope_type": "org", "limit_amount": 5},
            {"scope_type": "team", "team_id": team, "limit_amount": 5},
        ):
            resp = await client.put(
                "/v1/admin/budgets",
                json={"period": "day", "unit": "usd", **body},
                headers=bearer(who),
            )
            assert resp.status_code == 403, resp.text

    async def put(body: dict[str, Any]) -> int:
        resp = await client.put(
            "/v1/admin/budgets",
            json={"period": "day", "unit": "usd", **body},
            headers=bearer(token),
        )
        return resp.status_code

    assert await put({"scope_type": "team", "limit_amount": 5}) == 422  # no team
    assert await put({"scope_type": "org", "team_id": team, "limit_amount": 5}) == 422
    assert await put({"scope_type": "org", "limit_amount": -1}) == 422
    assert await put({"scope_type": "org", "limit_amount": 10**15 + 1}) == 422
    assert await put({"scope_type": "org", "limit_amount": 5, "soft_pct": 0}) == 422
    assert await put({"scope_type": "team", "team_id": str(uuid4()), "limit_amount": 5}) == 404
    other_admin, other_team, _ = await _org(client)
    assert await put({"scope_type": "team", "team_id": other_team, "limit_amount": 5}) == 404
    listed = await client.get("/v1/admin/budgets", headers=bearer(member))
    assert listed.status_code == 403
    theirs = await client.get("/v1/admin/budgets", headers=bearer(other_admin["access_token"]))
    assert theirs.json()["data"] == []  # nothing of this org leaks into another


async def test_limit_changes_are_audited_with_old_and_new_values(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, _ = await _org(client)
    token = admin["access_token"]
    first = await _set(client, token, scope_type="team", team_id=team, limit_amount=100)
    await _set(
        client, token, scope_type="team", team_id=team, limit_amount=200, reason="launch week"
    )
    deleted = await client.delete(f"/v1/admin/budgets/{first['id']}", headers=bearer(token))
    assert deleted.status_code == 204
    with psycopg.connect(test_db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT action, details, actor_id FROM audit_log WHERE target_id = %s ORDER BY seq",
            (first["id"],),
        ).fetchall()
    assert [r[0] for r in rows] == ["budgets.limit_set", "budgets.limit_set", "budgets.deleted"]
    assert (rows[0][1]["old_limit"], rows[0][1]["new_limit"]) == (None, 100)
    assert (rows[1][1]["old_limit"], rows[1][1]["new_limit"]) == (100, 200)
    assert rows[1][1]["reason"] == "launch week"
    assert rows[1][1]["team_id"] == team
    assert rows[2][1]["old_limit"] == 200
    assert {str(r[2]) for r in rows} == {admin["user"]["id"]}


async def test_budget_periods_follow_the_org_timezone(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin, team, _ = await _org(client)
    token = admin["access_token"]
    org_id = UUID(admin["org"]["id"])
    member = app.state.container.tokens.issue_access(add_user(test_db, org_id), org_id)
    put = await client.put(
        "/v1/admin/budget-settings", json={"timezone": "Asia/Kolkata"}, headers=bearer(token)
    )
    assert put.json() == {"timezone": "Asia/Kolkata", "allocation": "overcommit"}
    bad = await client.put(
        "/v1/admin/budget-settings", json={"timezone": "Mars/Olympus"}, headers=bearer(token)
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_timezone"
    denied = await client.put(
        "/v1/admin/budget-settings", json={"timezone": "UTC"}, headers=bearer(member)
    )
    assert denied.status_code == 403
    budget = await _set(
        client, token, scope_type="team", team_id=team, period="month", limit_amount=10
    )
    local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    assert date.fromisoformat(budget["period_start"]) == local_today.replace(day=1)
    with psycopg.connect(test_db.superuser_url) as conn:
        late_utc = "2026-11-30 20:00:00+00"  # already 1 Dec in Kolkata (UTC+5:30)
        row = conn.execute(
            "SELECT budget_period_start('month', %s, 'Asia/Kolkata'),"
            " budget_period_start('month', %s, 'UTC'),"
            " budget_period_start('day', %s, 'Asia/Kolkata'),"
            " budget_period_start('week', '2026-11-05 12:00+00', 'UTC'),"
            " budget_period_end('month', '2026-12-01'), budget_period_end('week', '2026-11-02')",
            (late_utc, late_utc, late_utc),
        ).fetchone()
    assert row == (
        date(2026, 12, 1),
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2026, 11, 2),
        date(2027, 1, 1),
        date(2026, 11, 9),
    )
