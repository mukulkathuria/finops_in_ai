"""Optimization stages (US-26): a shadow run leaves the request untouched, `on` hands the result
on but only for the version whose eval passed, a failing stage is reported and skipped, and the
whitespace compaction keeps code and indentation."""

import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from gateway.domain.compression import compact
from gateway.domain.llm import CanonicalRequest, Message, TextPart
from gateway.domain.pricing import ModelPrice
from gateway.domain.stages import RequestContext, StageReport, StageSetting, stage_settings
from gateway.pipeline.stages import CompressionStage, StageRunner

PRICE = ModelPrice(1, "mock", "m", "chat", price_input=1_000_000, price_output=2_000_000)


def _ctx(text: str = "hello", system: str = "be brief") -> RequestContext:
    messages = (Message("system", (TextPart(system),)), Message("user", (TextPart(text),)))
    return RequestContext(uuid4(), CanonicalRequest("m", messages, max_tokens=10), PRICE)


class _Shout:
    name, version, phase, default_mode = "shout", "1", "pre_llm", "shadow"

    async def process(self, ctx: RequestContext) -> StageReport:
        ctx.request = replace(ctx.request, messages=(Message("user", (TextPart("HELLO"),)),))
        return StageReport(self.name, self.version, "pre_llm", 10, 2, -8, applied=True)


class _Broken:
    name, version, phase, default_mode = "broken", "1", "pre_llm", "shadow"

    async def process(self, ctx: RequestContext) -> StageReport:
        ctx.request = replace(ctx.request, max_tokens=1)  # changed before failing: must not leak
        raise RuntimeError("boom")


class _Modes:
    def __init__(self, **settings: StageSetting) -> None:
        self.settings = settings

    async def get(self, org_id: object) -> dict[str, StageSetting]:
        return self.settings


async def test_a_shadow_stage_reports_what_it_would_save_and_changes_nothing() -> None:
    ctx = _ctx()
    original = ctx.request
    (report,) = await StageRunner([_Shout()], _Modes()).run(ctx, "pre_llm")
    assert ctx.request is original
    assert (report.shadow, report.applied, report.cost_delta_micro_usd) == (True, True, -8)


async def test_on_hands_the_result_on_only_for_the_version_whose_eval_passed() -> None:
    ctx = _ctx()
    (report,) = await StageRunner([_Shout()], _Modes(shout=StageSetting("on", "1"))).run(
        ctx, "pre_llm"
    )
    assert ctx.request.messages[0].text() == "HELLO"
    assert report.shadow is False
    stale = _ctx()  # the eval passed for another version: this one runs in shadow
    (report,) = await StageRunner([_Shout()], _Modes(shout=StageSetting("on", "0.9"))).run(
        stale, "pre_llm"
    )
    assert stale.request.messages[1].text() == "hello"
    assert report.shadow is True


async def test_a_failing_stage_is_reported_and_skipped_and_the_rest_still_run() -> None:
    ctx = _ctx()
    on = StageSetting("on", "1")
    runner = StageRunner([_Broken(), _Shout()], _Modes(broken=on, shout=on))
    broken, shout = await runner.run(ctx, "pre_llm")
    assert (broken.applied, broken.decisions) == (False, {"error": "failed"})
    assert shout.applied
    assert ctx.request.max_tokens == 10  # the failed stage's change never reached the request
    assert ctx.request.messages[0].text() == "HELLO"


async def test_off_skips_a_stage_and_the_registry_is_checked() -> None:
    ctx = _ctx()
    off = _Modes(shout=StageSetting("off"))
    assert await StageRunner([_Shout()], off).run(ctx, "pre_llm") == []
    assert await StageRunner([_Shout()], _Modes()).run(ctx, "post_llm") == []
    with pytest.raises(ValueError, match="unique"):
        StageRunner([_Shout(), _Shout()], _Modes())
    eager: Any = _Shout()
    eager.default_mode = "on"
    with pytest.raises(ValueError, match="eval"):
        StageRunner([eager], _Modes())
    stored = {"a": {"mode": "on", "version": "2"}, "b": "on", "c": {"mode": "x"}}
    assert stage_settings(stored) == {"a": StageSetting("on", "2")}


def test_compaction_keeps_code_and_indentation() -> None:
    text = "Summary:   the  plan   \n\n\n\n    indented  line\n```py\nx  =  1   \n```\nend  "
    assert compact(text) == "Summary: the plan\n\n    indented line\n```py\nx  =  1   \n```\nend"
    assert compact("no change here") == "no change here"


def test_compaction_stays_linear_on_long_runs() -> None:
    started = time.perf_counter()
    assert compact(" " * 1_000_000 + "x") == " " * 1_000_000 + "x"  # leading: indentation
    assert compact("x" + " " * 1_000_000 + "y") == "x y"
    assert compact("```\n" * 50_000) == "```\n" * 50_000
    assert time.perf_counter() - started < 5.0


async def test_the_compression_stage_prices_what_it_saves_and_leaves_system_text() -> None:
    ctx = _ctx(text="Q3    revenue   grew.\n\n\n\nThanks  ", system="keep  this   as  written")
    report = await CompressionStage().process(ctx)
    assert ctx.request.messages[1].text() == "Q3 revenue grew.\n\nThanks"
    assert ctx.request.messages[0].text() == "keep  this   as  written"
    assert report.applied
    assert report.tokens_after < report.tokens_before
    assert report.cost_delta_micro_usd < 0
    assert report.decisions == {"method": "whitespace"}
    tidy = _ctx(text="already tidy")
    assert not (await CompressionStage().process(tidy)).applied
