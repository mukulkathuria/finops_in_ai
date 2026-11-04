"""US-26: a new optimization stage registers by config, runs in shadow mode, shows up in the
stage reports, and is switched on only after passing the eval gate. The stage under test is the
whitespace compression stage, which ships in shadow mode."""

import json
from typing import Any
from uuid import UUID

import httpx2
import psycopg
from fastapi import FastAPI

from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

MESSY = "Summarise   this:\n\n\n\nQ3    revenue   grew  by  12 percent."
TIDY = "Summarise this:\n\nQ3 revenue grew by 12 percent."
EVAL = {
    "dataset": "support-prompts-v1",
    "metric": "answer_match",
    "threshold": 0.95,
    "samples": 200,
}


async def _chat(client: httpx2.AsyncClient, key: str) -> httpx2.Response:
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "mock/mock-small", "messages": [{"role": "user", "content": MESSY}]},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _sent_to_model(log: list[str]) -> str:
    """The user text of the one chat request the mock model received."""
    (body,) = [b for b in log if '"messages"' in b and "mock-small" in b]
    content: Any = json.loads(body)["messages"][-1]["content"]
    return content if isinstance(content, str) else "".join(p["text"] for p in content)


def _reports(db: DisposableDatabase, request_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return conn.execute(
            "SELECT stage, shadow, applied, tokens_before, tokens_after, cost_delta_micro_usd"
            " FROM stage_reports WHERE request_id = %s",
            (request_id,),
        ).fetchall()


async def test_a_new_stage_runs_in_shadow_and_goes_on_only_after_its_eval_passes(
    test_db: DisposableDatabase,
) -> None:
    log: list[str] = []
    async with app_client(app_settings(test_db), provider_log=log) as (_app, client):
        admin = await signup(client)
        auth = bearer(admin["access_token"])
        key = await _team_key(client, admin)
        listed = (await client.get("/v1/admin/stages", headers=auth)).json()["data"]
        assert [(s["name"], s["mode"], s["latest_eval"]) for s in listed] == [
            ("compression", "shadow", None),
            ("router", "off", None),  # US-19: ships off
        ]

        log.clear()
        shadow = await _chat(client, key)
        assert _sent_to_model(log) == MESSY  # shadow: the request went out untouched
        ((name, is_shadow, applied, before, after, delta),) = _reports(
            test_db, shadow.headers["x-request-id"]
        )
        assert (name, is_shadow, applied) == ("compression", True, True)
        assert after < before
        assert delta <= 0

        stages = "/v1/admin/stages/compression"
        refused = await client.put(stages, json={"mode": "on"}, headers=auth)
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            409,
            "eval_gate_not_passed",
        )
        failed = await client.post(f"{stages}/evals", json={**EVAL, "score": 0.91}, headers=auth)
        assert (failed.status_code, failed.json()["passed"]) == (201, False)
        assert (await client.put(stages, json={"mode": "on"}, headers=auth)).status_code == 409
        passed = await client.post(f"{stages}/evals", json={**EVAL, "score": 0.97}, headers=auth)
        assert passed.json()["passed"] is True
        enabled = await client.put(stages, json={"mode": "on"}, headers=auth)
        assert enabled.status_code == 200, enabled.text
        assert (enabled.json()["mode"], enabled.json()["latest_eval"]["score"]) == ("on", 0.97)

        log.clear()
        live = await _chat(client, key)
        assert _sent_to_model(log) == TIDY
        ((_, is_shadow, applied, *_),) = _reports(test_db, live.headers["x-request-id"])
        assert (is_shadow, applied) == (False, True)

    with psycopg.connect(test_db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE org_id = %s"
            " AND action IN ('stages.mode_set', 'evals.recorded') ORDER BY seq",
            (admin["org"]["id"],),
        ).fetchall()
    assert [r[0] for r in rows] == ["evals.recorded", "evals.recorded", "stages.mode_set"]


async def test_only_org_admins_change_stages_and_unknown_ones_are_not_found(
    api: tuple[FastAPI, httpx2.AsyncClient], test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    auth = bearer(admin["access_token"])
    org = UUID(admin["org"]["id"])
    member = bearer(app.state.container.tokens.issue_access(add_user(test_db, org), org))
    stages = "/v1/admin/stages/compression"
    assert (await client.put(stages, json={"mode": "off"}, headers=member)).status_code == 403
    denied = await client.post(f"{stages}/evals", json={**EVAL, "score": 1}, headers=member)
    assert denied.status_code == 403
    missing = await client.put("/v1/admin/stages/teleport", json={"mode": "off"}, headers=auth)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "stage_not_found")
    assert (await client.put(stages, json={"mode": "maybe"}, headers=auth)).status_code == 422
    off = await client.put(stages, json={"mode": "off"}, headers=auth)
    assert (off.status_code, off.json()["mode"]) == (200, "off")
