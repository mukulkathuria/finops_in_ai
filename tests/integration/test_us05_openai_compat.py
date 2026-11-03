"""US-05: an OpenAI-compatible endpoint.

Acceptance: the official OpenAI SDK works by changing the base URL; chat, streaming and
embeddings return valid shapes; usage and cost headers are present.
"""

import math
from typing import Any
from uuid import UUID, uuid4

import httpx2
import openai
import psycopg
import pytest
from fastapi import FastAPI
from mock_llm.app import embedding_for

from gateway.domain.llm import Usage
from gateway.domain.pricing import per_million
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase
from tests.support.users import add_user

Api = tuple[FastAPI, httpx2.AsyncClient]
SMALL_IN, SMALL_OUT = 150_000, 600_000  # mock/mock-small seed prices (micro-USD per 1M tokens)


async def _team_key(client: httpx2.AsyncClient) -> tuple[dict[str, Any], str, str]:
    admin = await signup(client)
    team = (
        await client.post(
            "/v1/admin/teams",
            json={"name": f"T {uuid4().hex[:6]}"},
            headers=bearer(admin["access_token"]),
        )
    ).json()["id"]
    key = (
        await client.post(
            f"/v1/admin/teams/{team}/api-keys",
            json={"name": "sdk"},
            headers=bearer(admin["access_token"]),
        )
    ).json()["key"]
    return admin, team, key


def _sdk(app: FastAPI, key: str) -> openai.AsyncOpenAI:
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    return openai.AsyncOpenAI(
        base_url="http://gateway.test/v1", api_key=key, http_client=http, max_retries=0
    )


def _ledger(db: DisposableDatabase, request_id: str) -> tuple[Any, ...]:
    with psycopg.connect(db.superuser_url) as conn:
        row = conn.execute(
            "SELECT status, kind, provider, model, input_tokens, output_tokens, cost_micro_usd,"
            " team_id, api_key_id FROM usage_events WHERE request_id = %s",
            (request_id,),
        ).fetchone()
    assert row is not None, f"no ledger row for {request_id}"
    return tuple(row)


async def test_official_sdk_chat_with_cost_headers_and_ledger(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    _, team, key = await _team_key(client)
    raw = await _sdk(app, key).chat.completions.with_raw_response.create(
        model="mock/mock-small", messages=[{"role": "user", "content": "hello gateway"}]
    )
    completion = raw.parse()
    assert completion.choices[0].message.content == "Mock answer from mock-small: hello gateway"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.model == "mock/mock-small"
    usage = completion.usage
    assert usage is not None
    expected = per_million(usage.prompt_tokens, SMALL_IN) + per_million(
        usage.completion_tokens, SMALL_OUT
    )
    assert raw.headers["x-finops-cost-micro-usd"] == str(expected)
    assert raw.headers["x-finops-provider"] == "mock"
    status, kind, provider, model, inp, out, cost, ledger_team, ledger_key = _ledger(
        test_db, raw.headers["x-request-id"]
    )
    assert (status, kind, provider, model) == ("ok", "chat", "mock", "mock-small")
    assert (inp, out, cost) == (usage.prompt_tokens, usage.completion_tokens, expected)
    assert str(ledger_team) == team
    assert ledger_key is not None


async def test_official_sdk_streaming_with_usage(api: Api, test_db: DisposableDatabase) -> None:
    app, client = api
    _, _, key = await _team_key(client)
    stream = await _sdk(app, key).chat.completions.create(
        model="mock/mock-small",
        messages=[{"role": "user", "content": "stream please"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, finish, usage = "", None, None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
            finish = chunk.choices[0].finish_reason or finish
        if chunk.usage:
            usage = chunk.usage
    assert text == "Mock answer from mock-small: stream please"
    assert finish == "stop"
    assert usage is not None
    assert usage.completion_tokens > 0


async def test_raw_stream_ends_with_cost_comment_and_done(api: Api) -> None:
    _, client = api
    _, _, key = await _team_key(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=bearer(key),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = [line for line in resp.text.split("\n") if line]
    assert lines[-1] == "data: [DONE]"
    assert lines[-2].startswith(": finops cost_micro_usd=")
    assert int(lines[-2].rsplit("=", 1)[1]) > 0


async def test_official_sdk_embeddings_use_base64_by_default(api: Api) -> None:
    app, client = api
    _, _, key = await _team_key(client)
    result = await _sdk(app, key).embeddings.create(
        model="mock/mock-embed", input=["alpha", "beta"]
    )
    assert [len(d.embedding) for d in result.data] == [1024, 1024]
    expected = embedding_for("alpha", 1024)
    assert all(
        math.isclose(a, b, abs_tol=1e-6)
        for a, b in zip(result.data[0].embedding[:8], expected[:8], strict=True)
    )
    assert result.usage.prompt_tokens > 0


async def test_models_lists_only_configured_providers(api: Api) -> None:
    app, client = api
    _, _, key = await _team_key(client)
    ids = {m.id async for m in _sdk(app, key).models.list()}
    assert {"mock/mock-small", "mock/mock-large", "mock/mock-embed"} <= ids
    assert not any(i.startswith("anthropic/") for i in ids)  # no ANTHROPIC_API_KEY configured


async def test_tool_calls_pass_through(api: Api) -> None:
    _, client = api
    _, _, key = await _team_key(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": "weather? [[mock:tool_call]]"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {"type": "object"}},
                }
            ],
        },
    )
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [
        ("rate_limit", 429, "upstream_rate_limited"),
        ("ctx_overflow", 400, "context_length_exceeded"),
        ("content_filter", 400, "content_filter"),
        ("server_error", 503, "upstream_unavailable"),
        ("auth", 502, "upstream_auth_failed"),
    ],
)
async def test_provider_failures_map_to_typed_errors_and_are_recorded(
    api: Api, test_db: DisposableDatabase, fault: str, status: int, code: str
) -> None:
    _, client = api
    _, _, key = await _team_key(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={
            "model": "mock/mock-small",
            "messages": [{"role": "user", "content": f"[[mock:{fault}]]"}],
        },
    )
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code
    if fault == "rate_limit":
        assert resp.headers["retry-after"] == "1"
    if fault == "auth":
        assert "Incorrect API key" not in resp.text  # upstream auth messages are never relayed
    assert _ledger(test_db, resp.headers["x-request-id"])[:1] == ("error",)


async def test_unknown_and_unconfigured_models(api: Api) -> None:
    _, client = api
    _, _, key = await _team_key(client)
    unknown = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "nope/nothing", "messages": [{"role": "user", "content": "x"}]},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"
    no_key = await client.post(
        "/v1/chat/completions",
        headers=bearer(key),
        json={"model": "anthropic/claude-opus-5-5", "messages": [{"role": "user", "content": "x"}]},
    )
    assert no_key.status_code == 503
    assert no_key.json()["error"]["code"] == "provider_not_configured"


@pytest.mark.parametrize(
    ("content", "status", "code"),
    [
        (b"{not json", 400, "invalid_json"),
        (
            b'{"model": "mock/mock-small", "messages": [{"role": "user", "content": "\xff\xfe"}]}',
            400,
            "invalid_json",
        ),
        (b"[" * 5000 + b"]" * 5000, 400, "invalid_json"),
        (
            b'{"model": "mock/mock-small", "messages": [{"role": "user", "content": "x"}], "n": 2}',
            422,
            "invalid_request",
        ),
        (b'{"model": "mock/mock-small", "messages": []}', 422, "invalid_request"),
        (
            b'{"model": "mock/mock-small", "messages": [{"role": "wizard", "content": "x"}]}',
            422,
            "invalid_request",
        ),
    ],
)
async def test_ingress_rejects_malformed_bodies_without_crashing(
    api: Api, content: bytes, status: int, code: str
) -> None:
    _, client = api
    _, _, key = await _team_key(client)
    resp = await client.post(
        "/v1/chat/completions",
        content=content,
        headers={**bearer(key), "content-type": "application/json"},
    )
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


async def test_oversized_bodies_get_413(test_db: DisposableDatabase) -> None:
    async with app_client(app_settings(test_db, max_request_bytes=2048)) as (_, client):
        _, _, key = await _team_key(client)
        resp = await client.post(
            "/v1/chat/completions",
            headers=bearer(key),
            json={
                "model": "mock/mock-small",
                "messages": [{"role": "user", "content": "x" * 5000}],
            },
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "request_too_large"


async def test_callers_without_model_access_are_denied(
    api: Api, test_db: DisposableDatabase
) -> None:
    app, client = api
    admin = await signup(client)
    org = UUID(admin["org"]["id"])
    finance_only = app.state.container.tokens.issue_access(
        add_user(test_db, org, roles=(("finance", None),)), org
    )
    resp = await client.post(
        "/v1/chat/completions",
        headers=bearer(finance_only),
        json={"model": "mock/mock-small", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 403


def test_usage_rejects_negative_or_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="never negative"):
        Usage(input_tokens=-1)
    with pytest.raises(ValueError, match="subsets"):
        Usage(input_tokens=5, cached_tokens=6)
