"""Phase 3 review regressions against PostgreSQL (docs/audits/2026-11-04-phase3-review.md)."""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi import FastAPI

from gateway.adapters.postgres.database import Database
from gateway.domain.llm import CanonicalRequest, Message, TextPart
from gateway.pipeline.chat import Attribution
from tests.integration.test_phase1_review import _counters, _ledger_rows, _team
from tests.integration.test_us14_files import _team_key
from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from worker.jobs.cache_janitor import CacheJanitor

Api = tuple[FastAPI, httpx2.AsyncClient]
EVAL = {"dataset": "regression-set", "metric": "match", "threshold": 0.9, "samples": 10}


async def test_m1_a_disconnect_during_the_cache_lookup_releases_the_hold(
    api: Api, test_db: DisposableDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    semantic = await client.put(
        "/v1/admin/cache-settings", json={"mode": "semantic"}, headers=bearer(token)
    )
    assert semantic.status_code == 200, semantic.text
    chat = app.state.container.chat_service
    entered = asyncio.Event()

    class _Stuck:
        model = "mock-embed"

        async def embed(self, texts: Any) -> list[list[float]]:
            entered.set()
            await asyncio.Event().wait()  # the embedder never answers
            raise AssertionError("unreachable")

    monkeypatch.setattr(chat._cache, "_embedder", _Stuck())
    resolved = await chat.resolve("mock/mock-small", "chat", org_id)
    question = Message("user", (TextPart("what is a budget hold?"),))
    req = CanonicalRequest(model=resolved.entry.model, messages=(question,), max_tokens=8)
    who = Attribution(org_id, UUID(team), None, None, f"req-{uuid4().hex}")
    task = asyncio.create_task(chat.complete(who, resolved, req))
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert _counters(test_db, budget["id"])[1] > 0  # held during the lookup
    task.cancel()  # the client goes away
    with pytest.raises(asyncio.CancelledError):
        await task
    await chat.drain()
    assert _counters(test_db, budget["id"])[1] == 0
    assert [status for status, _ in _ledger_rows(test_db, team)] == ["cancelled"]


async def test_the_eval_gate_follows_the_latest_run(api: Api, test_db: DisposableDatabase) -> None:
    _, client = api
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    stage = "/v1/admin/stages/compression"

    async def run(score: float) -> bool:
        resp = await client.post(f"{stage}/evals", json={**EVAL, "score": score}, headers=auth)
        assert resp.status_code == 201, resp.text
        return bool(resp.json()["passed"])

    assert await run(0.95)
    assert not await run(0.5)
    refused = await client.put(stage, json={"mode": "on"}, headers=auth)
    assert refused.status_code == 409, refused.text  # an earlier pass no longer counts
    assert await run(0.95)
    enabled = await client.put(stage, json={"mode": "on"}, headers=auth)
    assert enabled.status_code == 200, enabled.text
    assert not await run(0.5)  # a regression takes the stage back to shadow
    listed = (await client.get("/v1/admin/stages", headers=auth)).json()["data"]
    assert next(s for s in listed if s["name"] == "compression")["mode"] == "shadow"
    with psycopg.connect(test_db.superuser_url) as conn:
        row = conn.execute(
            "SELECT details FROM audit_log WHERE org_id = %s AND action = 'stages.mode_set'"
            " ORDER BY seq DESC LIMIT 1",
            (admin["org"]["id"],),
        ).fetchone()
    assert row is not None
    assert (row[0]["new"], row[0]["reason"]) == ("shadow", "eval_failed")


async def test_cached_answers_are_sealed_and_the_worker_only_cleans_up(
    api: Api, test_db: DisposableDatabase, worker_db: Database
) -> None:
    _, client = api
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    org = admin["org"]["id"]
    exact = await client.put("/v1/admin/cache-settings", json={"mode": "exact"}, headers=auth)
    assert exact.status_code == 200, exact.text
    key = bearer(await _team_key(client, admin))

    async def ask(text: str) -> httpx2.Response:
        body = {"model": "mock/mock-small", "messages": [{"role": "user", "content": text}]}
        resp = await client.post("/v1/chat/completions", headers=key, json=body)
        assert resp.status_code == 200, resp.text
        return resp

    first = await ask("seal me")
    again = await ask("seal me")
    assert again.headers["x-finops-cache"] == "exact"
    answer = first.json()["choices"][0]["message"]["content"]
    assert again.json()["choices"][0]["message"]["content"] == answer

    def entries() -> list[bytes]:
        with psycopg.connect(test_db.superuser_url) as conn:
            rows = conn.execute(
                "SELECT ciphertext FROM cache_entries WHERE org_id = %s ORDER BY created_at",
                (org,),
            ).fetchall()
        return [bytes(r[0]) for r in rows]

    assert all(answer.encode() not in sealed for sealed in entries())  # never in plain text
    with (
        psycopg.connect(test_db.worker_url) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT ciphertext FROM cache_entries LIMIT 1")

    for text in ("two", "three", "four"):
        await ask(text)
    assert len(entries()) == 4
    report = await CacheJanitor(worker_db, max_entries_per_org=2).run_once()
    assert report.evicted >= 2
    assert len(entries()) == 2  # the oldest went first
    with psycopg.connect(test_db.superuser_url) as conn:
        conn.execute(
            "UPDATE cache_entries SET expires_at = now() - interval '1 minute' WHERE org_id = %s",
            (org,),
        )
    assert (await CacheJanitor(worker_db, max_entries_per_org=1_000).run_once()).purged >= 2
    assert entries() == []
