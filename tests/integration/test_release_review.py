"""Release review (docs/audits/2026-11-05-release.md): regression tests for the billing
findings. The others are tested with their stories (test_us03_sessions.py,
test_us08_member_limits.py, test_us27_sso.py)."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from gateway.domain.llm import (
    CanonicalChunk,
    CanonicalRequest,
    CanonicalResponse,
    Message,
    TextPart,
    Usage,
)
from gateway.pipeline.chat import Attribution, ChatService, Resolved, estimate_tokens
from gateway.ports.llm import LLMAdapter
from tests.support.app import bearer, signup
from tests.support.database import DisposableDatabase

Api = tuple[FastAPI, httpx2.AsyncClient]


class _Held:
    """The mock provider, except that its answer, or its stream's first chunk, waits for the
    test to release it."""

    def __init__(self, inner: LLMAdapter) -> None:
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def complete(self, req: CanonicalRequest) -> CanonicalResponse:
        self.started.set()
        await self.release.wait()
        answer = await self.inner.complete(req)
        return replace(answer, usage=Usage(input_tokens=5, output_tokens=7))

    async def stream(self, req: CanonicalRequest) -> AsyncIterator[CanonicalChunk]:
        self.started.set()
        await self.release.wait()
        async for chunk in self.inner.stream(req):
            yield chunk


async def _team(client: httpx2.AsyncClient, token: str) -> str:
    made = await client.post(
        "/v1/admin/teams", json={"name": f"T {uuid4().hex[:6]}"}, headers=bearer(token)
    )
    assert made.status_code == 201, made.text
    team: str = made.json()["id"]
    return team


def _ledger(db: DisposableDatabase, team: str) -> list[tuple[str, int]]:
    with psycopg.connect(db.superuser_url) as conn:
        rows = conn.execute(
            "SELECT status, input_tokens + output_tokens FROM usage_events WHERE team_id = %s",
            (team,),
        ).fetchall()
    return [(str(status), int(tokens)) for status, tokens in rows]


async def _held_call(
    api: Api,
) -> tuple[ChatService, Resolved, _Held, CanonicalRequest, Attribution, str]:
    app, client = api
    admin = await signup(client)
    org_id = UUID(admin["org"]["id"])
    team = await _team(client, admin["access_token"])
    chat: ChatService = app.state.container.chat_service
    resolved = await chat.resolve("mock/mock-small", "chat", org_id)
    held = _Held(resolved.adapter)
    req = CanonicalRequest(
        model=resolved.entry.model, messages=(Message("user", (TextPart("hi"),)),), max_tokens=8
    )
    who = Attribution(org_id, UUID(team), None, None, f"req-{uuid4().hex}")
    return chat, replace(resolved, adapter=cast(LLMAdapter, held)), held, req, who, team


async def test_h1_a_completion_whose_client_left_is_charged_what_the_provider_reports(
    api: Api, test_db: DisposableDatabase
) -> None:
    """High H1: a disconnect during the provider call was recorded as free, although the
    provider answers and bills it, so a caller could spend past every budget."""
    chat, resolved, held, req, who, team = await _held_call(api)
    task = asyncio.create_task(chat.complete(who, resolved, req))
    await asyncio.wait_for(held.started.wait(), timeout=10)
    task.cancel()  # the client went away while the provider was answering
    with pytest.raises(asyncio.CancelledError):
        await task
    held.release.set()  # the provider answers anyway
    await chat.drain()
    assert _ledger(test_db, team) == [("cancelled", 12)]  # what the provider reported


async def test_h1_a_stream_cancelled_while_opening_is_charged_its_prompt(
    api: Api, test_db: DisposableDatabase
) -> None:
    """A stream cancelled before its first chunk is stopped and charged its estimated prompt,
    as a stream cancelled later already was."""
    chat, resolved, held, req, who, team = await _held_call(api)
    task = asyncio.create_task(chat.stream(who, resolved, req, []))
    await asyncio.wait_for(held.started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await chat.drain()
    assert _ledger(test_db, team) == [("cancelled", estimate_tokens("hi"))]


def test_estimates_do_not_undercount_text_outside_ascii() -> None:
    """Medium M1 (security): about 4 characters a token undercounted Chinese, near a token a
    character, four to six times. English text is counted as before."""
    assert estimate_tokens("hello world, how are you") == 6
    assert estimate_tokens("你好世界" * 25) >= 100  # 100 characters, about 100 tokens


async def test_m5_an_answer_whose_settlement_fails_still_reaches_the_client(
    api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Medium M5: when the ledger could not be written after the provider answered, the client
    got a 500 and retried, paying twice, and the charge was recorded nowhere."""
    chat, resolved, held, req, who, _ = await _held_call(api)
    held.release.set()  # the provider answers at once

    async def unreachable(*args: Any, **kwargs: Any) -> int:
        raise OperationalError("UPDATE budget_usage", {}, Exception("the database went away"))

    monkeypatch.setattr(chat._budgets, "settle", unreachable)
    result = await chat.complete(who, resolved, req)
    assert result.response.usage == Usage(input_tokens=5, output_tokens=7)
    lost = [r for r in caplog.records if "not in the ledger" in r.getMessage()]
    assert [r.__dict__["unrecorded"]["output_tokens"] for r in lost] == [7]  # reconcilable
