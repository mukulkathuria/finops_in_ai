"""Transactional outbox and alert webhooks (spec sections 4 and 6, F9; review finding D-8)."""

import hashlib
import hmac
import json
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
from fastapi import FastAPI

from gateway.adapters.postgres.database import Database
from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user
from worker.jobs.outbox_dispatcher import OutboxDispatcher

Api = tuple[FastAPI, httpx2.AsyncClient]


async def _org_with_alerting_budget(client: httpx2.AsyncClient) -> tuple[dict[str, Any], str, str]:
    """An org whose team budget crosses its soft threshold on the first request."""
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
    budget = await client.put(
        "/v1/admin/budgets",
        json={
            "scope_type": "team",
            "team_id": team,
            "period": "month",
            "unit": "tokens",
            "limit_amount": 1000,
            "soft_pct": 1,
        },
        headers=bearer(token),
    )
    assert budget.status_code == 201
    return admin, team, key


def _outbox(db: DisposableDatabase, org_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.superuser_url) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT topic, payload, attempts, delivered_at IS NOT NULL, failed_at IS NOT NULL,"
                " last_error FROM outbox WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            ).fetchall()
        ]


class Receiver:
    """A webhook receiver behind an httpx2 mock transport: records requests, answers `status`."""

    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status)


async def test_a_soft_alert_is_written_to_the_outbox_and_delivered_signed(
    api: Api, test_db: DisposableDatabase, worker_db: Database
) -> None:
    app, client = api
    admin, team, key = await _org_with_alerting_budget(client)
    token, org_id = admin["access_token"], admin["org"]["id"]

    created = await client.put(
        "/v1/admin/webhook",
        json={"url": "https://hooks.example.com/T1/B2/abc"},
        headers=bearer(token),
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    secret = created.json()["secret"]
    assert secret.startswith("whsec_")
    shown = (await client.get("/v1/admin/webhook", headers=bearer(token))).json()
    assert shown["url_hint"] == "https://hooks.example.com/…"
    assert shown["secret"] is None  # never shown again
    changed = await client.put(
        "/v1/admin/webhook",
        json={"url": "https://hooks.example.com/T1/B2/def"},
        headers=bearer(token),
    )
    assert changed.status_code == 200
    assert changed.json()["secret"] is None  # the URL changes, the secret stays

    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={
            "model": "mock/mock-small",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    (row,) = _outbox(test_db, org_id)
    assert row[0] == "budget.soft_threshold"
    assert row[1]["team_id"] == team
    assert row[1]["limit_amount"] == 1000

    receiver = Receiver()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(receiver.handle))
    dispatcher = OutboxDispatcher(
        worker_db, app.state.container.envelope, http, allow_insecure=True
    )
    report = await dispatcher.run_once()
    assert report.delivered >= 1
    (request,) = [r for r in receiver.requests if r.url.host == "hooks.example.com"]
    assert str(request.url) == "https://hooks.example.com/T1/B2/def"
    body = request.content
    expected = hmac.new(
        secret.encode(),
        request.headers["x-finops-timestamp"].encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["x-finops-signature"] == f"v1={expected}"
    assert request.headers["x-finops-event"] == "budget.soft_threshold"
    event = json.loads(body)
    assert event["type"] == "budget.soft_threshold"
    assert event["id"] == request.headers["x-finops-delivery"]
    assert event["data"]["budget_id"]
    assert _outbox(test_db, org_id)[0][3] is True  # delivered
    deliveries = (await client.get("/v1/admin/webhook/deliveries", headers=bearer(token))).json()
    assert deliveries["data"][0]["delivered_at"] is not None


async def test_failed_deliveries_back_off_and_unsafe_hosts_fail_for_good(
    api: Api, test_db: DisposableDatabase, worker_db: Database
) -> None:
    app, client = api
    admin, _, key = await _org_with_alerting_budget(client)
    token, org_id = admin["access_token"], admin["org"]["id"]
    await client.put(
        "/v1/admin/webhook", json={"url": "https://down.example.com/hook"}, headers=bearer(token)
    )
    assert (
        await client.post(
            "/v1/chat/completions",
            headers=bearer(key),
            json={
                "model": "mock/mock-small",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    ).status_code == 200

    receiver = Receiver(status=503)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(receiver.handle))
    envelope = app.state.container.envelope
    await OutboxDispatcher(worker_db, envelope, http, allow_insecure=True).run_once()
    (row,) = _outbox(test_db, org_id)
    assert (row[2], row[3], row[4], row[5]) == (1, False, False, "HTTP 503")
    with psycopg.connect(test_db.superuser_url) as conn:  # the backoff moved it into the future
        due = conn.execute(
            "SELECT available_at > now() + interval '20 seconds' FROM outbox WHERE org_id = %s",
            (org_id,),
        ).fetchone()
        assert due == (True,)
        conn.execute("UPDATE outbox SET available_at = now() WHERE org_id = %s", (org_id,))

    # The production policy refuses a receiver whose host resolves to a loopback address.
    await client.put(
        "/v1/admin/webhook", json={"url": "https://localhost/hook"}, headers=bearer(token)
    )
    strict = OutboxDispatcher(worker_db, envelope, http, allow_insecure=False)
    await strict.run_once()
    (row,) = _outbox(test_db, org_id)
    assert row[4] is True  # failed for good: no retries against internal hosts
    assert "non-public" in (row[5] or "")
    assert not [r for r in receiver.requests if r.url.host == "localhost"]


async def test_only_org_admins_manage_the_webhook(api: Api, test_db: DisposableDatabase) -> None:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    member = app.state.container.tokens.issue_access(add_user(test_db, org_id), org_id)
    for method, path, body in (
        ("PUT", "/v1/admin/webhook", {"url": "https://hooks.example.com/x"}),
        ("GET", "/v1/admin/webhook", None),
        ("POST", "/v1/admin/webhook/secret", None),
        ("DELETE", "/v1/admin/webhook", None),
        ("GET", "/v1/admin/webhook/deliveries", None),
    ):
        resp = await client.request(method, path, json=body, headers=bearer(member))
        assert resp.status_code == 403, (method, path)
    token = admin["access_token"]
    assert (await client.get("/v1/admin/webhook", headers=bearer(token))).status_code == 404
    bad = await client.put(
        "/v1/admin/webhook", json={"url": "ftp://hooks.example.com/x"}, headers=bearer(token)
    )
    assert bad.status_code == 422
    first = (
        await client.put(
            "/v1/admin/webhook", json={"url": "https://hooks.example.com/x"}, headers=bearer(token)
        )
    ).json()["secret"]
    rotated = (await client.post("/v1/admin/webhook/secret", headers=bearer(token))).json()[
        "secret"
    ]
    assert rotated != first
    assert (await client.delete("/v1/admin/webhook", headers=bearer(token))).status_code == 204
    with psycopg.connect(test_db.superuser_url) as conn:
        details = [
            r[0]
            for r in conn.execute(
                "SELECT details FROM audit_log WHERE org_id = %s AND action = 'settings.changed'"
                " ORDER BY seq",
                (org_id,),
            ).fetchall()
        ]
    assert [d["change"] for d in details] == ["set", "secret_rotated", "deleted"]
    assert all("hooks.example.com/x" not in json.dumps(d) for d in details)  # no paths logged
