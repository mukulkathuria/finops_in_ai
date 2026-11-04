"""Phase 3 review regressions that need no database (docs/audits/2026-11-04-phase3-review.md)."""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx2
import pytest

from gateway.domain.cache import cache_keys
from gateway.domain.compression import compact
from gateway.domain.llm import CanonicalRequest, Message, TextPart, Usage
from gateway.domain.price_list import PriceEntry, plan_sync
from gateway.domain.pricing import ModelPrice
from gateway.domain.savings import cache_hit_report, unserved
from gateway.domain.stages import RequestContext, StageReport
from gateway.pipeline.chat import ChatService
from gateway.pipeline.stages import CompressionStage
from worker.jobs.price_sync import PriceSync, parse_price_list

PRICE = ModelPrice(1, "mock", "m", "chat", 1_000_000, 4_000_000, price_cached_input=100_000)


def _request(*messages: Message) -> CanonicalRequest:
    return CanonicalRequest("m", messages, 10)


def test_l2_compaction_leaves_tabs_alone() -> None:
    assert compact("a\t\tb\t") == "a\t\tb\t"  # an empty field and a trailing one stay
    assert compact("x   y  ") == "x y"


async def test_l2_the_compression_stage_compacts_only_what_users_wrote() -> None:
    tool = Message("tool", (TextPart('{"name": "x  y"}'),), tool_call_id="c1")
    ctx = RequestContext(uuid4(), _request(Message("user", (TextPart("a   b"),)), tool), PRICE)
    stage = CompressionStage()
    await stage.process(ctx)
    assert [m.text() for m in ctx.request.messages] == ["a b", '{"name": "x  y"}']
    assert stage.version == "1.1"  # new behaviour: shadow until its own eval passes


def test_l4_a_cache_hit_saves_its_full_price() -> None:
    made = Usage(1_000, 100, cached_tokens=900)  # the answer was cheap thanks to the provider
    report = cache_hit_report("exact", made, None, PRICE)
    assert report.cost_delta_micro_usd == -PRICE.cost(Usage(1_000, 100))
    assert report.decisions == {}
    similar = cache_hit_report("semantic", made, 0.981234, PRICE)
    assert similar.decisions == {"similarity": 0.9812}


def test_l5_a_request_that_was_not_served_saves_nothing() -> None:
    (report,) = unserved([StageReport("router", "1.0", "pre_llm", 0, 0, -500, applied=True)])
    assert (report.cost_delta_micro_usd, report.applied) == (0, False)  # re-review: not counted


def test_cache_keys_change_with_the_detection_rules() -> None:
    req = _request(Message("user", (TextPart("hi"),)))
    scope: dict[str, Any] = {
        "org_id": uuid4(),
        "team_id": None,
        "user_id": None,
        "req": req,
        "policy": {},
    }
    assert cache_keys(**scope, rules="rules-1") != cache_keys(**scope, rules="rules-2")


def _bare_chat() -> ChatService:
    unused: dict[str, Any] = dict.fromkeys(
        (
            "db",
            "guardrails",
            "catalog",
            "usage",
            "budgets",
            "idempotency",
            "telemetry",
            "stages",
            "stage_reports",
            "overhead",
            "baselines",
            "cache",
        )
    )
    return ChatService(adapters={}, default_max_tokens=1, retries=0, verifier=object(), **unused)  # type: ignore[arg-type]


async def test_l6_drain_waits_for_late_writes_and_starts_no_quality_check() -> None:
    chat = _bare_chat()
    done: list[str] = []

    async def late() -> None:
        await asyncio.sleep(0.01)
        done.append("late")

    async def first() -> None:
        await asyncio.sleep(0.01)
        chat._spawn(late())  # a settlement that starts while draining
        done.append("first")

    chat._spawn(first())
    draining = asyncio.create_task(chat.drain())
    await asyncio.sleep(0)
    judged = StageReport(
        "router", "1.0", "pre_llm", 0, 0, 0, applied=True, decisions={"judge": True}
    )
    chat._verify(SimpleNamespace(reports=[judged]), None, None)  # type: ignore[arg-type]
    await draining
    assert done == ["first", "late"]
    assert chat._judging == 0  # no quality check started while draining


def test_security_l6_a_new_model_without_any_price_is_refused() -> None:
    free = PriceEntry(
        ModelPrice(0, "acme", "free", "chat", 0, 0, context_window=8, max_output_tokens=4)
    )
    plan = plan_sync([free], {}, datetime.now(UTC))
    assert plan.inserts == ()
    assert plan.refused == ((free, "a new model would have no price at all"),)


def test_security_l6_a_chat_model_needs_its_limits() -> None:
    raw = (
        b'{"models": [{"provider": "acme", "model": "m", "kind": "chat", "price_input": 1,'
        b' "price_output": 1, "context_window": 0, "max_output_tokens": 0}]}'
    )
    with pytest.raises(ValueError, match="invalid price list"):
        parse_price_list(raw)


async def test_security_l5_a_failed_download_never_names_the_url() -> None:
    url = "https://ops:" + "pw" + "@prices.example.test/list.json?token=" + "t0ken"

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused for " + str(request.url), request=request)

    for transport in (
        httpx2.MockTransport(lambda request: httpx2.Response(500)),
        httpx2.MockTransport(refuse),
    ):
        async with httpx2.AsyncClient(transport=transport) as http:
            sync = PriceSync(None, url, http=http)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="fetching the price list failed") as err:
                await sync._load()
        assert "t0ken" not in str(err.value)
        # Nothing chained (and so no URL) would be printed with the traceback.
        assert err.value.__cause__ is None
        assert err.value.__context__ is None or err.value.__suppress_context__


async def test_rereview_a_malformed_source_url_is_not_logged_either() -> None:
    url = "https://prices.example.test/list.json?token=" + "t0ken"

    class _Http:
        def stream(self, *_: Any, **__: Any) -> Any:
            raise httpx2.InvalidURL(f"Invalid URL {url!r}")  # not an HTTPError

    sync = PriceSync(None, url, http=_Http())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fetching the price list failed: InvalidURL") as err:
        await sync._load()
    assert "t0ken" not in str(err.value)
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__


def test_security_l7_litellm_is_loaded_for_a_server() -> None:
    pytest.importorskip("litellm")
    from gateway.adapters.llm.litellm_adapter import load_litellm

    litellm = load_litellm()
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"  # no .env file is read into the process
    assert litellm.turn_off_message_logging is True


async def test_l8_a_litellm_stream_is_closed_when_the_client_goes_away() -> None:
    pytest.importorskip("litellm")
    from gateway.adapters.llm.litellm_adapter import LiteLLMAdapter

    adapter = LiteLLMAdapter(base_url="https://x.test/v1", api_key="k", http=httpx2.AsyncClient())
    closed: list[bool] = []
    event = {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}

    class _Chunks:
        def __aiter__(self) -> _Chunks:
            return self

        async def __anext__(self) -> SimpleNamespace:
            return SimpleNamespace(model_dump=lambda: event)

        async def aclose(self) -> None:
            closed.append(True)

    async def acompletion(**_: Any) -> _Chunks:
        return _Chunks()

    adapter._litellm = SimpleNamespace(acompletion=acompletion)
    stream = adapter.stream(_request(Message("user", (TextPart("hi"),))))
    assert (await anext(stream)).content == "hi"
    await stream.aclose()  # the client went away mid-stream
    assert closed == [True]
