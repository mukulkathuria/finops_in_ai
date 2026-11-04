"""US-18: an identical request hits the exact cache; a similar question above the threshold hits
the semantic cache; another org or permission scope never hits. Every request the gateway sends
the mock model is recorded, so a hit is proven by the model not being asked again."""

from typing import Any

import httpx2
import psycopg

from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

QUESTION = "What is the capital of France?"


def _chat_body(text: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": "mock/mock-small",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 40,
        "stream": stream,
    }


def _model_calls(log: list[str]) -> int:
    return sum(1 for body in log if '"messages"' in body and "mock-small" in body)


async def _org(client: httpx2.AsyncClient, mode: str) -> tuple[dict[str, Any], list[str]]:
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    resp = await client.put("/v1/admin/cache-settings", json={"mode": mode}, headers=auth)
    assert resp.status_code == 200, resp.text
    keys = [await _team_key(client, admin) for _ in range(2)]  # two teams of the same org
    return admin, keys


def _ledger(db: DisposableDatabase, org: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return conn.execute(
            "SELECT cache_hit, cost_micro_usd, baseline_micro_usd FROM usage_events"
            " WHERE org_id = %s ORDER BY ts",
            (org,),
        ).fetchall()


async def test_identical_requests_hit_and_no_other_scope_or_org_ever_does(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (_app, client):
        admin, (team_a, team_b) = await _org(client, "exact")
        first = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION), headers=bearer(team_a)
        )
        assert first.status_code == 200, first.text
        assert "x-finops-cache" not in first.headers
        calls = _model_calls(log)

        again = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION), headers=bearer(team_a)
        )
        assert again.headers["x-finops-cache"] == "exact"
        assert (
            again.json()["choices"][0]["message"]["content"]
            == (first.json()["choices"][0]["message"]["content"])
        )
        assert _model_calls(log) == calls  # served without asking the model

        streamed = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION, stream=True), headers=bearer(team_a)
        )
        assert streamed.headers["x-finops-cache"] == "exact"
        assert _model_calls(log) == calls

        other_team = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION), headers=bearer(team_b)
        )
        assert "x-finops-cache" not in other_team.headers  # another permission scope
        other_admin, (other_org, _) = await _org(client, "exact")
        stranger = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION), headers=bearer(other_org)
        )
        assert "x-finops-cache" not in stranger.headers  # another org
        assert other_admin["org"]["id"] != admin["org"]["id"]

    rows = _ledger(test_db, admin["org"]["id"])
    assert [r[0] for r in rows] == ["none", "exact", "exact", "none"]
    (_, miss_cost, _), (_, hit_cost, hit_baseline) = rows[0], rows[1]
    assert miss_cost > 0
    assert hit_cost == 0
    assert hit_baseline > 0  # what the answer would have cost: it shows as saved


async def test_a_similar_question_hits_the_semantic_cache_and_a_different_one_does_not(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (_app, client):
        _admin, (key, _) = await _org(client, "semantic")
        first = await client.post(
            "/v1/chat/completions", json=_chat_body(QUESTION), headers=bearer(key)
        )
        assert first.status_code == 200, first.text
        calls = _model_calls(log)
        similar = await client.post(
            "/v1/chat/completions",
            json=_chat_body("what is the CAPITAL of france"),
            headers=bearer(key),
        )
        assert similar.headers["x-finops-cache"] == "semantic"
        assert _model_calls(log) == calls
        different = await client.post(
            "/v1/chat/completions",
            json=_chat_body("What is the capital of Spain?"),
            headers=bearer(key),
        )
        assert "x-finops-cache" not in different.headers
        assert _model_calls(log) == calls + 1
        conversation = {
            **_chat_body(QUESTION),
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": QUESTION},
            ],
        }
        multi = await client.post("/v1/chat/completions", json=conversation, headers=bearer(key))
        assert "x-finops-cache" not in multi.headers  # a history changes what an answer means
