"""US-06 and Phase 3, the switch test: moving a provider between the native and the LiteLLM
adapter is one line of configuration. Both answer the same requests the same way, streamed or
not, and record the same usage and cost; the ledger names the adapter that served."""

from typing import Any

import orjson
import psycopg
import pytest

from tests.integration.test_us14_files import _team_key
from tests.support.app import app_client, app_settings, bearer, signup
from tests.support.database import DisposableDatabase

pytest.importorskip("litellm")  # the optional extra (ADR 0008)

QUESTION = {"model": "mock/mock-small", "messages": [{"role": "user", "content": "hello switch"}]}


def _streamed_text(body: str) -> str:
    text = []
    for line in body.splitlines():
        if line.startswith("data: {"):
            for choice in orjson.loads(line[6:])["choices"]:
                text.append(choice["delta"].get("content") or "")
    return "".join(text)


async def _serve(db: DisposableDatabase, adapter: str) -> dict[str, Any]:
    settings = app_settings(db, provider_adapter_default=adapter)
    async with app_client(settings) as (_app, client):
        admin = await signup(client)
        auth = bearer(await _team_key(client, admin))
        chat = await client.post("/v1/chat/completions", headers=auth, json=QUESTION)
        streamed = await client.post(
            "/v1/chat/completions",
            headers=auth,
            json={**QUESTION, "stream": True, "stream_options": {"include_usage": True}},
        )
        embedded = await client.post(
            "/v1/embeddings",
            headers=auth,
            json={"model": "mock/mock-embed", "input": ["hello switch"]},
        )
    for resp in (chat, streamed, embedded):
        assert resp.status_code == 200, resp.text
    with psycopg.connect(db.superuser_url) as conn:
        ledger = conn.execute(
            "SELECT kind, adapter, input_tokens, output_tokens, cost_micro_usd, status"
            " FROM usage_events WHERE org_id = %s ORDER BY ts, kind",
            (admin["org"]["id"],),
        ).fetchall()
    return {
        "answer": chat.json()["choices"][0]["message"]["content"],
        "usage": chat.json()["usage"],
        "model": chat.headers["x-finops-model"],
        "stream": _streamed_text(streamed.text),
        "vector": embedded.json()["data"][0]["embedding"],
        "ledger": ledger,
    }


async def test_switching_a_provider_to_litellm_is_configuration_only(
    test_db: DisposableDatabase,
) -> None:
    native = await _serve(test_db, "native")
    litellm = await _serve(test_db, "litellm")
    assert native["answer"] == "Mock answer from mock-small: hello switch"
    for field in ("answer", "usage", "model", "stream", "vector"):
        assert litellm[field] == native[field], field
    assert [row[1] for row in native["ledger"]] == ["mock"] * 3
    assert [row[1] for row in litellm["ledger"]] == ["litellm:mock"] * 3
    same = [(kind, *rest) for kind, _adapter, *rest in native["ledger"]]
    assert [(kind, *rest) for kind, _adapter, *rest in litellm["ledger"]] == same
