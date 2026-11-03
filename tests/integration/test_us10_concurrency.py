"""US-10: concurrent requests never exceed a hard limit.

Acceptance: under 500 parallel requests against a small budget, total committed never exceeds
the limit. Also spec section 6 and 11: the same Idempotency-Key sent 50 times charges once; the
janitor returns holds of crashed requests and rebuilds counters from the ledger.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from gateway.adapters.postgres.budgets import BudgetRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.domain.budgets import Demand
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from worker.__main__ import HEARTBEAT, run
from worker.jobs.budget_janitor import BudgetJanitor

Api = tuple[FastAPI, httpx2.AsyncClient]
DEMAND = 2 + 16 + 10  # the worst case a 10-token request for "hi" holds (see test_us07)


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


def _body(content: str = "hi", **extra: Any) -> dict[str, Any]:
    return {
        "model": "mock/mock-small",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 10,
        **extra,
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


def _ledger(db: DisposableDatabase, team: str) -> tuple[int, int, int]:
    """(ok requests, tokens, micro-USD) the ledger holds for one team."""
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE status = 'ok'),"
            " coalesce(sum(input_tokens + output_tokens), 0), coalesce(sum(cost_micro_usd), 0)"
            " FROM usage_events WHERE team_id = %s",
            (team,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


async def test_500_parallel_requests_never_overshoot_a_small_budget(
    test_db: DisposableDatabase,
) -> None:
    settings = app_settings(
        test_db, db_pool_size=30, db_max_overflow=10, db_pool_timeout_seconds=60
    )
    async with app_client(settings) as (app, client):
        admin, team, key = await _org(client)
        token = admin["access_token"]
        tokens = await _set(
            client, token, scope_type="team", team_id=team, limit_amount=20 * DEMAND
        )
        money = await _set(client, token, scope_type="org", unit="usd", limit_amount=400)

        responses = await asyncio.gather(
            *(
                client.post("/v1/chat/completions", json=_body(), headers=bearer(key))
                for _ in range(500)
            )
        )
        await app.state.container.chat_service.drain()

    codes = Counter(r.status_code for r in responses)
    assert set(codes) <= {200, 429}, codes  # no deadlock, timeout or internal error
    assert codes[200] > 0
    assert codes[429] > 0
    scopes = Counter(r.json()["error"]["scope"]["type"] for r in responses if r.status_code == 429)
    assert set(scopes) <= {"team", "org"}

    ok, ledger_tokens, ledger_cost = _ledger(test_db, team)
    assert ok == codes[200]
    committed_tokens, reserved_tokens = _counters(test_db, tokens["id"])
    committed_cost, reserved_cost = _counters(test_db, money["id"])
    assert committed_tokens <= 20 * DEMAND  # zero overshoot
    assert committed_cost <= 400
    assert (committed_tokens, reserved_tokens) == (ledger_tokens, 0)
    assert (committed_cost, reserved_cost) == (ledger_cost, 0)


async def test_the_same_idempotency_key_sent_50_times_charges_once(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    token = admin["access_token"]
    budget = await _set(client, token, scope_type="team", team_id=team, limit_amount=10**6)
    headers = {**bearer(key), "Idempotency-Key": "order-42"}

    burst = await asyncio.gather(
        *(
            client.post("/v1/chat/completions", json=_body("once"), headers=headers)
            for _ in range(50)
        )
    )
    done = [r for r in burst if r.status_code == 200]
    busy = [r for r in burst if r.status_code == 409]
    assert len(done) + len(busy) == 50
    assert done
    assert {r.json()["error"]["code"] for r in busy} <= {"idempotency_in_progress"}
    assert len({r.json()["id"] for r in done}) == 1  # one completion, replayed to the rest

    retry = await client.post("/v1/chat/completions", json=_body("once"), headers=headers)
    assert retry.status_code == 200
    assert retry.headers["idempotent-replayed"] == "true"
    assert retry.json() == done[0].json()
    assert _ledger(test_db, team)[0] == 1
    assert _counters(test_db, budget["id"]) == (_ledger(test_db, team)[1], 0)

    reused = await client.post("/v1/chat/completions", json=_body("other"), headers=headers)
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "idempotency_key_reused"
    bad = await client.post(
        "/v1/chat/completions", json=_body(), headers={**bearer(key), "Idempotency-Key": "a b"}
    )
    assert bad.status_code == 422

    other_key = (
        await client.post(
            f"/v1/admin/teams/{team}/api-keys", json={"name": "k2"}, headers=bearer(token)
        )
    ).json()["key"]
    theirs = await client.post(
        "/v1/chat/completions",
        json=_body("once"),
        headers={**bearer(other_key), "Idempotency-Key": "order-42"},
    )
    assert theirs.status_code == 200
    assert "idempotent-replayed" not in theirs.headers  # keys are scoped to their credential
    assert _ledger(test_db, team)[0] == 2


async def test_failed_or_rejected_attempts_free_their_idempotency_key(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    token = admin["access_token"]

    async def send(content: str, idem: str) -> httpx2.Response:
        return await client.post(
            "/v1/chat/completions",
            json=_body(content),
            headers={**bearer(key), "Idempotency-Key": idem},
        )

    for _ in range(2):  # a provider failure is not stored: the retry runs again
        assert (await send("[[mock:server_error]]", "flaky")).status_code == 503
    with psycopg.connect(test_db.superuser_url) as conn:
        errors = conn.execute(
            "SELECT count(*) FROM usage_events WHERE team_id = %s AND status = 'error'", (team,)
        ).fetchone()
    assert errors == (2,)

    await _set(client, token, scope_type="team", team_id=team, unit="usd", limit_amount=1)
    assert (await send("hi", "late")).status_code == 429  # rejected before the key was kept
    await _set(client, token, scope_type="team", team_id=team, unit="usd", limit_amount=10**6)
    accepted = await send("hi", "late")
    assert accepted.status_code == 200
    assert "idempotent-replayed" not in accepted.headers


async def test_a_finished_stream_is_replayed_not_rerun(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    _, team, key = await _org(client)
    headers = {**bearer(key), "Idempotency-Key": "stream-1"}
    first = await client.post("/v1/chat/completions", json=_body(stream=True), headers=headers)
    await app.state.container.chat_service.drain()
    again = await client.post("/v1/chat/completions", json=_body(stream=True), headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.headers["idempotent-replayed"] == "true"

    def content(sse: str) -> tuple[str, set[str]]:
        text, ids = [], set()
        for line in sse.splitlines():
            if line.startswith("data: {"):
                event = httpx2.Response(200, content=line[6:]).json()
                ids.add(event["id"])
                for choice in event["choices"]:
                    text.append(choice["delta"].get("content") or "")
        return "".join(text), ids

    assert content(first.text) == content(again.text)
    assert again.text.endswith("data: [DONE]\n\n")
    assert _ledger(test_db, team)[0] == 1


async def test_the_janitor_returns_expired_holds_without_double_counting(
    api: Api, test_db: DisposableDatabase, app_db: Database, worker_db: Database
) -> None:
    _, client = api
    admin, team, _ = await _org(client)
    budget = await _set(
        client, admin["access_token"], scope_type="team", team_id=team, limit_amount=1000
    )
    org_id, now = UUID(admin["org"]["id"]), datetime.now(UTC)
    repo = BudgetRepository()
    async with app_db.transaction(TenantContext(org_id)) as conn:  # a request that then crashed
        await repo.reserve(
            conn,
            hold="crashed-1",
            team_id=UUID(team),
            user_id=None,
            demand=Demand(usd_micro=0, tokens=300),
            at=now,
            expires_at=now - timedelta(seconds=1),
        )
    assert _counters(test_db, budget["id"]) == (0, 300)

    assert await BudgetJanitor(worker_db).expire_holds() >= 1
    assert _counters(test_db, budget["id"]) == (0, 0)
    async with app_db.transaction(TenantContext(org_id)) as conn:  # its late settlement
        await repo.settle(conn, hold="crashed-1", actual=Demand(usd_micro=0, tokens=120))
    assert _counters(test_db, budget["id"]) == (120, 0)  # counted once, never subtracted twice


async def test_the_janitor_rebuilds_drifted_counters_from_the_ledger(
    api: Api, test_db: DisposableDatabase, worker_db: Database
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    budget = await _set(
        client, admin["access_token"], scope_type="team", team_id=team, limit_amount=10**6
    )
    for _ in range(3):
        resp = await client.post("/v1/chat/completions", json=_body(), headers=bearer(key))
        assert resp.status_code == 200
    truth = _counters(test_db, budget["id"])
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE budget_usage SET committed = committed + 777, reserved = 55"
            " WHERE budget_id = %s",
            (budget["id"],),
        )
    assert await BudgetJanitor(worker_db).reconcile() >= 1
    assert _counters(test_db, budget["id"]) == truth


async def test_a_new_budget_counts_spending_already_made_in_its_period(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin, team, key = await _org(client)
    for _ in range(2):
        resp = await client.post("/v1/chat/completions", json=_body(), headers=bearer(key))
        assert resp.status_code == 200
    budget = await _set(
        client, admin["access_token"], scope_type="team", team_id=team, limit_amount=10**6
    )
    assert budget["committed"] == _ledger(test_db, team)[1]


async def test_the_worker_purges_old_keys_and_beats(
    api: Api, test_db: DisposableDatabase, worker_db: Database
) -> None:
    _, client = api
    _, _, key = await _org(client)
    resp = await client.post(
        "/v1/chat/completions", json=_body(), headers={**bearer(key), "Idempotency-Key": "aged"}
    )
    assert resp.status_code == 200
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE idempotency_keys SET created_at = now() - interval '25 hours'"
            " WHERE key = 'aged'"
        )
    assert await BudgetJanitor(worker_db).purge_idempotency() >= 1

    HEARTBEAT.unlink(missing_ok=True)
    stop = asyncio.Event()
    settings = app_settings(test_db, worker_database_url=test_db.worker_url)
    worker = asyncio.create_task(run(settings, stop))
    for _ in range(100):
        if HEARTBEAT.exists():
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, timeout=10)
    assert HEARTBEAT.exists()
