"""US-19: easy prompts route to a smaller model; sampled judge scores show no quality drop beyond
the tolerance; a team can opt out. The router ships off, runs in shadow first (the small tier
answers sampled requests on the side) and runs on only after its eval passed."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx2
import psycopg

from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

EASY = "What is the capital of France?"
HARD = "Design and compare two caching algorithms step by step:\n```py\ncache = {}\n```"
TIERS = {
    "large_model": "mock/mock-large",
    "small_model": "mock/mock-small",
    "judge_rate": 1.0,
    "tolerance": 0.2,
}


def _window() -> dict[str, str]:
    today = datetime.now(UTC).date()
    return {"start": str(today - timedelta(days=1)), "end": str(today + timedelta(days=2))}


async def _team(client: httpx2.AsyncClient, auth: dict[str, str]) -> tuple[str, str]:
    team = await client.post("/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=auth)
    team_id = str(team.json()["id"])
    key = await client.post(f"/v1/admin/teams/{team_id}/api-keys", json={"name": "k"}, headers=auth)
    return team_id, str(key.json()["key"])


async def _ask(client: httpx2.AsyncClient, key: str, text: str) -> httpx2.Response:
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "mock/mock-large", "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _ledger(db: DisposableDatabase, org: str) -> dict[str, tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT request_id, model, route_tier, baseline_micro_usd, cost_micro_usd"
            " FROM usage_events WHERE org_id = %s",
            (org,),
        ).fetchall()
    return {r[0]: r[1:] for r in rows}


def _router_reports(db: DisposableDatabase, request_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return conn.execute(
            "SELECT shadow, applied, cost_delta_micro_usd, decisions FROM stage_reports"
            " WHERE request_id = %s AND stage = 'router'",
            (request_id,),
        ).fetchall()


async def _samples(db: DisposableDatabase, org: str, expected: int) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for _ in range(200):  # the other tier answers after the reply
        with psycopg.connect(db.superuser_url) as conn:
            rows = conn.execute(
                "SELECT request_id, candidate_model, reference_model, score FROM eval_samples"
                " WHERE org_id = %s ORDER BY created_at",
                (org,),
            ).fetchall()
        if len(rows) >= expected:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {expected} judged samples, found {len(rows)}")


async def test_routing_runs_in_shadow_then_on_judged_within_tolerance(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (_app, client):
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        org = admin["org"]["id"]
        _, key = await _team(client, auth)
        opted_team, opted_key = await _team(client, auth)
        settings = {**TIERS, "opt_out_teams": [opted_team]}
        resp = await client.put("/v1/admin/routing-settings", json=settings, headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["opt_out_teams"] == [opted_team]

        # Shadow: the large tier answers; the small one answers the sampled request on the side.
        router = "/v1/admin/stages/router"
        assert (await client.put(router, json={"mode": "shadow"}, headers=auth)).status_code == 200
        shadow = await _ask(client, key, EASY)
        assert shadow.json()["model"] == "mock/mock-large"
        (shadow_sample,) = await _samples(test_db, org, 1)

        # On: refused until the router's eval passed.
        refused = await client.put(router, json={"mode": "on"}, headers=auth)
        assert refused.status_code == 409, refused.text
        run = {"dataset": "routing-v1", "metric": "agreement", "score": 0.93, "threshold": 0.85}
        evals = await client.post(f"{router}/evals", json={**run, "samples": 200}, headers=auth)
        assert evals.status_code == 201, evals.text
        assert (await client.put(router, json={"mode": "on"}, headers=auth)).status_code == 200

        routed = await _ask(client, key, EASY)
        assert routed.json()["model"] == "mock/mock-small"
        assert routed.headers["x-finops-model"] == "mock/mock-small"
        await _samples(test_db, org, 2)
        hard = await _ask(client, key, HARD)
        kept = await _ask(client, opted_key, EASY)  # the team opted out
        assert (hard.json()["model"], kept.json()["model"]) == ("mock/mock-large",) * 2
        report = await client.get("/v1/reports/routing", params=_window(), headers=auth)

    ledger = _ledger(test_db, org)
    shadow_id, routed_id = shadow.headers["x-request-id"], routed.headers["x-request-id"]
    assert ledger[shadow_id][:2] == ("mock-large", None)  # shadow changes nothing
    assert ledger[f"{shadow_id}.judge"][:3] == ("mock-small", None, 0)  # overhead: no baseline
    model, tier, baseline, cost = ledger[routed_id]
    assert (model, tier) == ("mock-small", "small")
    assert baseline > cost > 0  # the baseline is the model asked for
    assert ledger[f"{routed_id}.judge"][:3] == ("mock-large", None, 0)
    assert ledger[hard.headers["x-request-id"]][:2] == ("mock-large", "large")
    assert ledger[kept.headers["x-request-id"]][:2] == ("mock-large", None)

    ((shadowed, applied, delta, decisions),) = _router_reports(test_db, shadow_id)
    assert (shadowed, applied, decisions["to"]) == (True, True, "mock/mock-small")
    assert delta < 0  # what it would have saved, on the tokens the request used
    ((shadowed, applied, delta, _),) = _router_reports(test_db, routed_id)
    assert (shadowed, applied) == (False, True)
    assert delta == cost - baseline

    samples = await _samples(test_db, org, 2)
    assert shadow_sample[:3] == (shadow_id, "mock/mock-small", "mock/mock-large")
    assert samples[1][:3] == (routed_id, "mock/mock-small", "mock/mock-large")
    assert all(row[3] >= 0.8 for row in samples)  # the mock answers differ by one word
    body = report.json()
    assert report.status_code == 200, report.text
    assert (body["samples"], body["within_tolerance"], body["below_tolerance"]) == (2, True, 0)
    assert (body["routed"], body["kept_on_large"]) == (1, 1)
    assert sum('"mock-small"' in b for b in log) == 2  # the shadow check and the routed answer


async def test_routing_settings_are_validated_and_admin_only(test_db: DisposableDatabase) -> None:
    async with app_client(app_settings(test_db)) as (_app, client):
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        bad = [
            {"large_model": "mock/mock-large", "small_model": "mock/mock-large"},
            {"large_model": "mock/mock-large"},
            {"large_model": "mock/mock-large", "small_model": "mock/nope"},
            {"large_model": "mock/mock-large", "small_model": "mock/mock-embed"},
            {"tolerance": 0.9},
            {"opt_out_teams": ["not-a-uuid"]},
            {"enabled": True},
        ]
        for body in bad:
            resp = await client.put("/v1/admin/routing-settings", json=body, headers=auth)
            assert resp.status_code in (400, 404, 422), (body, resp.text)
        current = await client.get("/v1/admin/routing-settings", headers=auth)
        assert current.json()["large_model"] is None  # nothing was stored
        _, key = await _team(client, auth)
        denied = await client.put("/v1/admin/routing-settings", json=TIERS, headers=bearer(key))
        assert denied.status_code in (401, 403)
