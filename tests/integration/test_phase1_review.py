"""Regression tests for the Phase 1 review findings (spec section 12: every fix adds a test that
fails before the fix and passes after it). Each test names the finding it pins."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.usage import UsageEvent
from gateway.domain.llm import CanonicalRequest, Message, TextPart, Usage
from gateway.pipeline.chat import Attribution
from tests.support.app import STRONG_PASSWORD, bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _team(client: httpx2.AsyncClient, token: str) -> str:
    resp = await client.post(
        "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
    )
    team: str = resp.json()["id"]
    return team


async def _join(client: httpx2.AsyncClient, token: str, team: str, user: UUID) -> None:
    resp = await client.post(
        f"/v1/admin/teams/{team}/members", json={"user_id": str(user)}, headers=bearer(token)
    )
    assert resp.status_code == 201, resp.text


def _chat(content: str = "hi") -> dict[str, Any]:
    return {
        "model": "mock/mock-small",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 10,
    }


def _counters(db: DisposableDatabase, budget_id: str) -> tuple[int, int]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(committed), 0), coalesce(sum(reserved), 0)"
            " FROM budget_usage WHERE budget_id = %s",
            (budget_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _ledger_rows(db: DisposableDatabase, team: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT status, input_tokens + output_tokens FROM usage_events WHERE team_id = %s",
                (team,),
            ).fetchall()
        ]


async def test_c1_a_member_of_several_teams_must_name_the_team(
    api: Api, test_db: DisposableDatabase
) -> None:
    """Critical C1: without X-FinOps-Team, team and member budgets were skipped."""
    app, client = api
    admin = await signup(client)
    token, org_id = admin["access_token"], UUID(admin["org"]["id"])
    team_a, team_b = await _team(client, token), await _team(client, token)
    user = add_user(test_db, org_id)
    for team in (team_a, team_b):
        await _join(client, token, team, user)
    budget = await client.put(
        "/v1/admin/budgets",
        json={
            "scope_type": "team",
            "team_id": team_a,
            "period": "day",
            "unit": "usd",
            "limit_amount": 1,
        },
        headers=bearer(token),
    )
    assert budget.status_code == 201
    jwt = app.state.container.tokens.issue_access(user, org_id)

    ambiguous = await client.post("/v1/chat/completions", json=_chat(), headers=bearer(jwt))
    assert ambiguous.status_code == 422
    assert ambiguous.json()["error"]["code"] == "team_required"
    named = await client.post(
        "/v1/chat/completions", json=_chat(), headers={**bearer(jwt), "X-FinOps-Team": team_a}
    )
    assert named.status_code == 429  # team A's budget now applies
    assert named.json()["error"]["scope"]["type"] == "team"
    other = await client.post(
        "/v1/chat/completions", json=_chat(), headers={**bearer(jwt), "X-FinOps-Team": team_b}
    )
    assert other.status_code == 200
    me = await client.get("/v1/whoami", headers=bearer(jwt))  # self-inspection needs no team
    assert me.status_code == 200
    assert me.json()["team_id"] is None


async def test_h1_a_stream_closed_before_its_first_read_still_settles(
    api: Api, test_db: DisposableDatabase
) -> None:
    """High H1: a relay closed before iteration never settled (no ledger row, hold kept)."""
    app, client = api
    admin = await signup(client)
    token, org_id = admin["access_token"], UUID(admin["org"]["id"])
    team = await _team(client, token)
    budget = (
        await client.put(
            "/v1/admin/budgets",
            json={
                "scope_type": "team",
                "team_id": team,
                "period": "day",
                "unit": "tokens",
                "limit_amount": 10**6,
            },
            headers=bearer(token),
        )
    ).json()
    chat = app.state.container.chat_service
    resolved = await chat.resolve("mock/mock-small", "chat", org_id)
    req = CanonicalRequest(
        model=resolved.entry.model, messages=(Message("user", (TextPart("hello"),)),), max_tokens=8
    )
    who = Attribution(org_id, UUID(team), None, None, f"req-{uuid4().hex}")
    result = await chat.stream(who, resolved, req, [])
    assert _counters(test_db, budget["id"])[1] > 0  # held while open
    await result.chunks.aclose()  # the client went away before the first byte
    await result.chunks.aclose()  # closing twice is harmless
    await chat.drain()
    assert _counters(test_db, budget["id"])[1] == 0
    assert [status for status, _ in _ledger_rows(test_db, team)] == ["cancelled"]


async def test_m1_settlement_survives_cancellation_of_the_request(
    api: Api, test_db: DisposableDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium M1: cancelling the handler during settlement rolled the charge back."""
    app, client = api
    admin = await signup(client)
    token, org_id = admin["access_token"], UUID(admin["org"]["id"])
    team = await _team(client, token)
    chat = app.state.container.chat_service
    repo = chat._budgets
    original = repo.settle
    entered = asyncio.Event()

    async def slow_settle(*args: Any, **kwargs: Any) -> int:
        entered.set()
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repo, "settle", slow_settle)
    resolved = await chat.resolve("mock/mock-small", "chat", org_id)
    req = CanonicalRequest(
        model=resolved.entry.model, messages=(Message("user", (TextPart("hi"),)),), max_tokens=8
    )
    who = Attribution(org_id, UUID(team), None, None, f"req-{uuid4().hex}")
    task = asyncio.create_task(chat.complete(who, resolved, req))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await chat.drain()
    rows = _ledger_rows(test_db, team)
    assert [status for status, _ in rows] == ["ok"]  # the charge was not lost


async def test_m2_settlement_retries_transient_failures_and_writes_one_row(
    api: Api, test_db: DisposableDatabase, app_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium M2: one dropped connection during settlement lost the charge (and returned 500)."""
    app, client = api
    admin = await signup(client)
    token = admin["access_token"]
    team = await _team(client, token)
    key = (
        await client.post(
            f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=bearer(token)
        )
    ).json()["key"]
    usage_repo = app.state.container.chat_service._usage
    original = usage_repo.record
    failures = {"left": 1}

    async def flaky_record(conn: Any, event: Any) -> None:
        await original(conn, event)
        if failures["left"]:
            failures["left"] -= 1
            raise OperationalError("COMMIT", {}, ConnectionError("connection lost"))

    monkeypatch.setattr(usage_repo, "record", flaky_record)
    resp = await client.post("/v1/chat/completions", json=_chat(), headers=bearer(key))
    assert resp.status_code == 200
    assert [status for status, _ in _ledger_rows(test_db, team)] == ["ok"]

    # A retry after an unacknowledged commit must not write the hold's ledger row twice.
    event = UsageEvent(
        org_id=UUID(admin["org"]["id"]),
        team_id=UUID(team),
        user_id=None,
        api_key_id=None,
        request_id="dup-check",
        kind="chat",
        provider="mock",
        model="mock-small",
        adapter="openai_compat",
        catalog_version=1,
        usage=Usage(input_tokens=1, output_tokens=1),
        cost_micro_usd=1,
        status="ok",
        latency_ms=1,
        ts=datetime.now(UTC),
        hold=str(uuid4()),
    )
    for _ in range(2):
        async with app_db.transaction(TenantContext(event.org_id)) as conn:
            await original(conn, event)
    with psycopg.connect(test_db.superuser_url) as conn:
        dupes = conn.execute(
            "SELECT count(*) FROM usage_events WHERE request_id = 'dup-check'"
        ).fetchone()
    assert dupes == (1,)


async def test_m4_m5_allocation_is_serialised_and_timezone_changes_keep_counters(
    api: Api, test_db: DisposableDatabase
) -> None:
    """M4: strict allocation raced on a team's first cap. M5: a timezone change reset counters."""
    _, client = api
    admin = await signup(client)
    token, org_id = admin["access_token"], UUID(admin["org"]["id"])
    settings = await client.put(
        "/v1/admin/budget-settings", json={"allocation": "strict"}, headers=bearer(token)
    )
    assert settings.status_code == 200
    for _ in range(5):
        team = await _team(client, token)
        users = [add_user(test_db, org_id) for _ in range(2)]
        for user in users:
            await _join(client, token, team, user)

        def body(
            scope: str, limit: int, user: UUID | None = None, team: str = team
        ) -> dict[str, Any]:
            out: dict[str, Any] = {
                "scope_type": scope,
                "team_id": team,
                "period": "day",
                "unit": "tokens",
                "limit_amount": limit,
            }
            if user is not None:
                out["user_id"] = str(user)
            return out

        await asyncio.gather(
            client.put("/v1/admin/budgets", json=body("team", 100), headers=bearer(token)),
            *(
                client.put("/v1/admin/budgets", json=body("member", 60, u), headers=bearer(token))
                for u in users
            ),
        )
        listed = (
            await client.get("/v1/admin/budgets", params={"team_id": team}, headers=bearer(token))
        ).json()["data"]
        cap = [b["limit_amount"] for b in listed if b["scope_type"] == "team"]
        members = sum(b["limit_amount"] for b in listed if b["scope_type"] == "member")
        assert not cap or members <= cap[0], listed  # strict: never above the cap

    team = await _team(client, token)
    key = (
        await client.post(
            f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=bearer(token)
        )
    ).json()["key"]
    budget = (
        await client.put(
            "/v1/admin/budgets",
            json={
                "scope_type": "team",
                "team_id": team,
                "period": "day",
                "unit": "tokens",
                "limit_amount": 10**6,
            },
            headers=bearer(token),
        )
    ).json()
    assert (
        await client.post("/v1/chat/completions", json=_chat(), headers=bearer(key))
    ).status_code == 200
    spent = budget["committed"] + sum(t for _, t in _ledger_rows(test_db, team))
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):  # UTC+14 and UTC-11
        moved = await client.put(
            "/v1/admin/budget-settings", json={"timezone": zone}, headers=bearer(token)
        )
        assert moved.status_code == 200
        (row,) = [
            b
            for b in (await client.get("/v1/admin/budgets", headers=bearer(token))).json()["data"]
            if b["id"] == budget["id"]
        ]
        assert row["committed"] == spent  # the new period starts from the ledger, not zero


async def test_security_findings_on_keys_leads_lockout_and_invites(
    api: Api, test_db: DisposableDatabase
) -> None:
    """Security M1 (keys bound to others), Low (lead removes lead), M3 (lockout oracle) and L1
    (existence leaked before authorization)."""
    app, client = api
    admin = await signup(client)
    token, org_id = admin["access_token"], UUID(admin["org"]["id"])
    team, other = await _team(client, token), await _team(client, token)
    lead_id = add_user(test_db, org_id, roles=(("member", None), ("team_lead", UUID(team))))
    peer_lead = add_user(test_db, org_id, roles=(("member", None), ("team_lead", UUID(team))))
    victim = add_user(test_db, org_id)
    for user in (lead_id, peer_lead, victim):
        await _join(client, token, team, user)
    lead = app.state.container.tokens.issue_access(lead_id, org_id)

    async def key_for(who: str, user: UUID) -> httpx2.Response:
        return await client.post(
            f"/v1/admin/teams/{team}/api-keys",
            json={"name": f"k-{uuid4().hex[:4]}", "user_id": str(user)},
            headers=bearer(who),
        )

    assert (await key_for(lead, victim)).status_code == 403  # spend billed to someone else
    assert (await key_for(lead, lead_id)).status_code == 201
    assert (await key_for(token, victim)).status_code == 201  # org admins may

    removed = await client.delete(
        f"/v1/admin/teams/{team}/members/{peer_lead}", headers=bearer(lead)
    )
    assert removed.status_code == 403
    assert (
        await client.delete(f"/v1/admin/teams/{team}/members/{peer_lead}", headers=bearer(token))
    ).status_code == 204

    invite = await client.post(
        "/v1/admin/invitations",
        json={
            "email": admin["user"]["email"],
            "team_id": other,
            "team_role": "member",
        },
        headers=bearer(lead),
    )
    assert invite.status_code == 403  # no 409: nothing about the account is revealed

    email = admin["user"]["email"]
    for _ in range(5):
        wrong = await client.post(
            "/v1/auth/login", json={"email": email, "password": "Wrong-Password-123"}
        )
        assert wrong.status_code == 401
    locked = await client.post("/v1/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    unknown = await client.post(
        "/v1/auth/login", json={"email": f"nobody-{uuid4().hex[:6]}@example.com", "password": "x"}
    )
    assert locked.status_code == unknown.status_code == 401  # a lock reveals no account
    assert locked.json()["error"] == unknown.json()["error"]
