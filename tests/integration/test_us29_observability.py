"""US-29: health, metrics and tracing.

Acceptance: readiness and liveness endpoints, Prometheus metrics, and a trace per request with
stage timings. No prompt or completion text ever reaches a span or a metric.
"""

from secrets import token_hex, token_urlsafe
from typing import Any
from uuid import uuid4

import httpx2
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client.parser import text_string_to_metric_families
from pydantic import SecretStr

from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]
STAGES = {"auth", "budget.reserve", "chat mock-small", "budget.settle"}


async def _key(client: httpx2.AsyncClient) -> tuple[str, str, str]:
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
    return token, team, key


def _chat(content: str, **extra: Any) -> dict[str, Any]:
    return {
        "model": "mock/mock-small",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 16,
        **extra,
    }


def _value(text: str, name: str, **labels: str) -> float:
    return sum(
        s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items())
    )


async def test_liveness_and_readiness(api: Api) -> None:
    _, client = api
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok"}


async def test_metrics_count_requests_tokens_cost_stages_and_refusals(api: Api) -> None:
    _, client = api
    admin, team, key = await _key(client)
    canary = f"PROMPT-CANARY-{uuid4().hex}"
    resp = await client.post("/v1/chat/completions", json=_chat(canary), headers=bearer(key))
    assert resp.status_code == 200
    await client.put(
        "/v1/admin/budgets",
        json={
            "scope_type": "team",
            "team_id": team,
            "period": "day",
            "unit": "usd",
            "limit_amount": 1,
        },
        headers=bearer(admin),
    )
    refused = await client.post("/v1/chat/completions", json=_chat("hi"), headers=bearer(key))
    assert refused.status_code == 429

    scrape = await client.get("/metrics")
    assert scrape.status_code == 200
    text = scrape.text
    model = {"provider": "mock", "model": "mock-small"}
    assert _value(text, "finops_llm_requests_total", kind="chat", status="ok", **model) >= 1
    assert _value(text, "finops_llm_tokens_total", direction="input", **model) > 0
    assert _value(text, "finops_llm_tokens_total", direction="output", **model) > 0
    assert _value(text, "finops_llm_cost_micro_usd_total", **model) > 0
    for stage in ("auth", "budget.reserve", "provider", "budget.settle"):
        assert _value(text, "finops_stage_duration_seconds_count", stage=stage) >= 1
    assert _value(text, "finops_budget_rejections_total", scope="team") >= 1
    route = {"method": "POST", "route": "/v1/chat/completions"}
    assert _value(text, "finops_http_requests_total", status="200", **route) >= 1
    assert _value(text, "finops_http_requests_total", status="429", **route) >= 1
    assert canary not in text
    assert resp.json()["choices"][0]["message"]["content"] not in text


async def test_metrics_need_the_configured_token(test_db: DisposableDatabase) -> None:
    secret = token_urlsafe(16)
    settings = app_settings(test_db, metrics_token=SecretStr(secret))
    async with app_client(settings) as (_, client):
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/metrics", headers=bearer("wrong"))).status_code == 401
        allowed = await client.get("/metrics", headers=bearer(secret))
        assert allowed.status_code == 200
        assert "finops_http_requests" in allowed.text


async def test_each_request_is_one_trace_with_timed_stage_spans(
    test_db: DisposableDatabase,
) -> None:
    exporter = InMemorySpanExporter()
    async with app_client(app_settings(test_db), span_exporter=exporter) as (app, client):
        _, _, key = await _key(client)
        exporter.clear()
        trace_id, parent_id = token_hex(16), token_hex(8)
        canary = f"PROMPT-CANARY-{uuid4().hex}"
        resp = await client.post(
            "/v1/chat/completions",
            json=_chat(canary),
            headers={**bearer(key), "traceparent": f"00-{trace_id}-{parent_id}-01"},
        )
        assert resp.status_code == 200
        assert resp.headers["x-trace-id"] == trace_id  # the caller's trace is continued

        streamed = await client.post(
            "/v1/chat/completions", json=_chat("stream me", stream=True), headers=bearer(key)
        )
        assert streamed.status_code == 200
        await app.state.container.chat_service.drain()

    spans = exporter.get_finished_spans()
    mine = [s for s in spans if s.context and format(s.context.trace_id, "032x") == trace_id]
    server = next(s for s in mine if s.kind == SpanKind.SERVER)
    assert server.name == "POST /v1/chat/completions"
    assert server.parent is not None
    assert format(server.parent.span_id, "016x") == parent_id
    assert server.attributes is not None
    assert server.attributes["http.route"] == "/v1/chat/completions"
    assert server.attributes["http.response.status_code"] == 200
    children = {
        s.name: s
        for s in mine
        if s.parent is not None and server.context and s.parent.span_id == server.context.span_id
    }
    assert set(children) >= STAGES  # every stage of the pipeline, timed
    for span in children.values():
        assert span.start_time is not None
        assert span.end_time is not None
        assert span.end_time >= span.start_time
    gen_ai = children["chat mock-small"]
    assert gen_ai.kind == SpanKind.CLIENT
    assert gen_ai.attributes is not None
    assert gen_ai.attributes["gen_ai.provider.name"] == "mock"
    assert gen_ai.attributes["gen_ai.request.model"] == "mock-small"
    assert gen_ai.attributes["gen_ai.usage.output_tokens"] > 0
    assert gen_ai.attributes["finops.cost_micro_usd"] > 0
    for span in spans:  # privacy: no prompt or completion text in any span
        for value in (span.attributes or {}).values():
            assert canary not in str(value)

    stream_trace = streamed.headers["x-trace-id"]
    stream_spans = {
        s.name: s for s in spans if s.context and format(s.context.trace_id, "032x") == stream_trace
    }
    assert {"POST /v1/chat/completions", "chat mock-small", "budget.settle"} <= set(stream_spans)
    stream_attributes = stream_spans["chat mock-small"].attributes
    assert stream_attributes is not None
    assert stream_attributes["gen_ai.usage.output_tokens"] > 0
