"""US-24: image generation with quotas. The image quota holds per member and per team, under
concurrency too; each image is priced from the catalog and counted in the ledger; moderation
refuses disallowed prompts before any spend and fails closed; the org's exact cache serves a
repeat at no cost, to the same caller only."""

import asyncio
from typing import Any
from uuid import uuid4

import httpx2
import psycopg
from fastapi import FastAPI
from pydantic import SecretStr

from tests.integration.test_us09_own_usage import _setup
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]
MODEL = "mock/mock-image"


async def _image(
    client: httpx2.AsyncClient, auth: dict[str, str], prompt: str = "a lighthouse", **extra: Any
) -> httpx2.Response:
    body = {"model": MODEL, "prompt": prompt, **extra}
    return await client.post("/v1/images/generations", headers=auth, json=body)


async def _team_and_key(client: httpx2.AsyncClient, admin: dict[str, str]) -> tuple[str, str]:
    team = (
        await client.post("/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=admin)
    ).json()["id"]
    created = await client.post(
        f"/v1/admin/teams/{team}/api-keys", json={"name": "k"}, headers=admin
    )
    return str(team), str(created.json()["key"])


async def _quota(
    client: httpx2.AsyncClient, admin: dict[str, str], amount: int, **scope: str
) -> None:
    body = {"period": "day", "unit": "images", "limit_amount": amount, **scope}
    resp = await client.put("/v1/admin/budgets", json=body, headers=admin)
    assert resp.status_code in (200, 201), resp.text  # 201 when the budget is new


def _ledger(db: DisposableDatabase, request_id: str) -> tuple[Any, ...]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT kind, status, images, cost_micro_usd, baseline_micro_usd, cache_hit"
            " FROM usage_events WHERE request_id = %s",
            (request_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


async def test_us24_images_are_priced_counted_and_held_to_member_and_team_quotas(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin_token, team, [(me, my_token), (_, other_token)] = await _setup(app, client, test_db)
    admin, mine, theirs = bearer(admin_token), bearer(my_token), bearer(other_token)
    await _quota(client, admin, 2, scope_type="member", team_id=team, user_id=str(me))

    made = await _image(client, mine, n=2, quality="hd")
    assert made.status_code == 200, made.text
    assert len(made.json()["data"]) == 2
    assert all(item["b64_json"] for item in made.json()["data"])
    assert made.headers["x-finops-cost-micro-usd"] == "160000"  # two hd images at 80,000
    request_id = made.headers["x-request-id"]
    assert _ledger(test_db, request_id) == ("image", "ok", 2, 160_000, 160_000, "none")

    over = await _image(client, mine)
    assert over.status_code == 429, over.text
    assert over.json()["error"]["code"] == "budget_exceeded"
    assert (await _image(client, theirs)).status_code == 200  # my quota is not theirs
    await _quota(client, admin, 3, scope_type="team", team_id=team)  # the team has made 3
    refused = await _image(client, theirs)
    assert refused.status_code == 429, refused.text
    unknown = await _image(client, theirs, size="512x512")
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "unsupported_image_option"


async def test_us24_the_image_quota_holds_under_concurrency(api: Api) -> None:
    _, client = api
    admin = bearer((await signup(client))["access_token"])
    team, key = await _team_and_key(client, admin)
    await _quota(client, admin, 3, scope_type="team", team_id=team)
    results = await asyncio.gather(*(_image(client, bearer(key)) for _ in range(6)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200, 200, 429, 429, 429], [r.text for r in results]


async def test_us24_moderation_refuses_before_any_spend_and_fails_closed(
    test_db: DisposableDatabase,
) -> None:
    async with app_client(app_settings(test_db)) as (_, client):  # the built-in rules
        key = bearer(
            (await _team_and_key(client, bearer((await signup(client))["access_token"])))[1]
        )
        refused = await _image(client, key, prompt="a battlefield full of gore")
        assert refused.status_code == 400, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["categories"]) == ("content_blocked", ["violence/graphic"])
        assert _ledger(test_db, refused.headers["x-request-id"])[1:4] == ("blocked", 0, 0)
    remote = app_settings(
        test_db,
        moderation_backend="openai_compat",
        moderation_base_url="http://mock-llm:8000/v1",
        moderation_api_key=SecretStr("test-key"),
    )
    async with app_client(remote) as (_, client):  # the mock's moderation API
        key = bearer(
            (await _team_and_key(client, bearer((await signup(client))["access_token"])))[1]
        )
        assert (await _image(client, key, prompt="a calm sea")).status_code == 200
        flagged = await _image(client, key, prompt="a calm sea [[mock:flagged]]")
        assert (flagged.status_code, flagged.json()["error"]["code"]) == (400, "content_blocked")
        down = await _image(client, key, prompt="a calm sea [[mock:moderation_down]]")
        assert down.status_code == 503, down.text  # no verdict, no image, no charge
        assert _ledger(test_db, down.headers["x-request-id"])[1:4] == ("error", 0, 0)


async def test_us24_a_repeat_comes_from_the_cache_for_the_same_caller_only(
    api: Api, test_db: DisposableDatabase
) -> None:
    _, client = api
    admin = bearer((await signup(client))["access_token"])
    cache = await client.put("/v1/admin/cache-settings", json={"mode": "exact"}, headers=admin)
    assert cache.status_code == 200, cache.text
    key = bearer((await _team_and_key(client, admin))[1])
    first = await _image(client, key, prompt="a red kite over hills")
    again = await _image(client, key, prompt="a red kite over hills")
    assert first.status_code == again.status_code == 200
    assert "x-finops-cache" not in first.headers
    assert again.headers["x-finops-cache"] == "exact"
    assert again.headers["x-finops-cost-micro-usd"] == "0"
    assert again.json()["data"] == first.json()["data"]
    assert _ledger(test_db, again.headers["x-request-id"]) == ("image", "ok", 0, 0, 40_000, "exact")
    other = bearer((await _team_and_key(client, admin))[1])  # another team: its own scope
    theirs = await _image(client, other, prompt="a red kite over hills")
    assert "x-finops-cache" not in theirs.headers
    assert theirs.headers["x-finops-cost-micro-usd"] == "40000"
