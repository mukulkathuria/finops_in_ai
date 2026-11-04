"""Routing (US-19): easy requests for the large tier go to the small one, demanding ones stay, a
team can opt out, a tier that cannot take the request never gets it, the judged share is
sampled, and the stage runner hands the cheaper model on only when the router is on."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from gateway.domain.llm import (
    CanonicalRequest,
    CanonicalResponse,
    Message,
    TextPart,
    ToolCall,
    ToolSpec,
    Usage,
)
from gateway.domain.pricing import ModelPrice
from gateway.domain.routing import RoutingSettings, complexity, fits
from gateway.domain.stages import RequestContext, StageSetting
from gateway.pipeline.idempotency import Origin, dump_chat, load_chat
from gateway.pipeline.routing import RouterStage, Verifier, said
from gateway.pipeline.stages import StageRunner

LARGE = ModelPrice(
    1, "mock", "mock-large", "chat", 2_500_000, 10_000_000,
    context_window=128_000, max_output_tokens=16_384,
)  # fmt: skip
SMALL = ModelPrice(
    1, "mock", "mock-small", "chat", 150_000, 600_000,
    context_window=8_192, max_output_tokens=4_096,
)  # fmt: skip
OTHER = ModelPrice(1, "acme", "tiny", "chat", 1, 1)
SETTINGS = RoutingSettings("mock/mock-large", "mock/mock-small", judge_rate=0.5)


def _req(text: str, max_tokens: int = 100, **fields: object) -> CanonicalRequest:
    messages = (Message("user", (TextPart(text),)),)
    return CanonicalRequest("mock-large", messages, max_tokens, **fields)  # type: ignore[arg-type]


def test_complexity_grows_with_length_code_tools_reasoning_and_depth() -> None:
    easy = complexity(_req("What is the capital of France?"))
    assert easy < 0.05
    assert complexity(_req("Debug this step by step:\n```py\nprint(1)\n```")) >= 0.35
    tool = ToolSpec("lookup", "find", {"type": "object"})
    assert complexity(_req("hi", tools=(tool,))) >= 0.3
    assert complexity(_req("word " * 4_000)) > easy
    assert complexity(_req("Analyze " * 20_000)) <= 1.0


def test_a_tier_fits_only_requests_it_can_take_whole() -> None:
    assert fits(SMALL, _req("hello", max_tokens=4_096))
    assert not fits(SMALL, _req("hello", max_tokens=4_097))  # more than it may write
    assert not fits(SMALL, _req("x" * 6_000, max_tokens=4_096))  # prompt and answer overflow
    assert fits(OTHER, _req("x" * 100_000, max_tokens=50_000))  # limits unknown: no ceiling


class _Catalog:
    def __init__(self, *entries: ModelPrice) -> None:
        self.entries = {e.public_name: e for e in entries}

    async def get(self) -> _Catalog:
        return self

    def resolve(self, name: str) -> ModelPrice | None:
        return self.entries.get(name)


class _Settings:
    def __init__(self, settings: RoutingSettings) -> None:
        self.settings = settings

    async def get(self, org_id: object) -> RoutingSettings:
        return self.settings


def _stage(settings: RoutingSettings, *catalog: ModelPrice, sample: float = 0.1) -> RouterStage:
    return RouterStage(_Settings(settings), _Catalog(*catalog), sample=lambda: sample)  # type: ignore[arg-type]


def _ctx(
    req: CanonicalRequest, price: ModelPrice = LARGE, team: UUID | None = None
) -> RequestContext:
    return RequestContext(uuid4(), req, price, team)


async def test_easy_requests_for_the_large_tier_go_to_the_small_one() -> None:
    ctx = _ctx(_req("What is the capital of France?"))
    report = await _stage(SETTINGS, SMALL, LARGE).process(ctx)
    assert report.applied
    assert (ctx.price, ctx.request.model) == (SMALL, "mock-small")
    assert report.decisions == {
        "tier": "small",
        "score": 0.002,  # 30 characters of plain text
        "from": "mock/mock-large",
        "to": "mock/mock-small",
        "judge": True,  # 0.1 < judge_rate
    }
    assert (report.tokens_before, report.tokens_after, report.cost_delta_micro_usd) == (0, 0, 0)
    unsampled = await _stage(SETTINGS, SMALL, LARGE, sample=0.9).process(_ctx(_req("hi")))
    assert "judge" not in unsampled.decisions


async def test_hard_opted_out_unfit_and_other_requests_keep_their_model() -> None:
    team = uuid4()
    hard = _req("Design and compare two algorithms step by step:\n```\ncode\n```")
    opted = RoutingSettings("mock/mock-large", "mock/mock-small", opt_out_teams=(str(team),))
    elsewhere = RoutingSettings("mock/mock-large", "acme/tiny")
    cases = [
        (SETTINGS, _ctx(hard), {"tier": "large"}),
        (SETTINGS, _ctx(_req("hi"), SMALL), {"skipped": "model"}),
        (RoutingSettings(), _ctx(_req("hi")), {"skipped": "model"}),
        (opted, _ctx(_req("hi"), team=team), {"skipped": "opted_out"}),
        (elsewhere, _ctx(_req("hi")), {"skipped": "small_model"}),  # another provider
        (SETTINGS, _ctx(_req("hi", max_tokens=8_000)), {"skipped": "small_model"}),
    ]
    for settings, ctx, expected in cases:
        before = (ctx.price, ctx.request)
        report = await _stage(settings, SMALL, LARGE, OTHER).process(ctx)
        assert not report.applied
        assert (ctx.price, ctx.request) == before
        assert expected.items() <= report.decisions.items()


class _Modes:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def get(self, org_id: object) -> dict[str, StageSetting]:
        return {"router": StageSetting(self.mode, "1.0")}  # type: ignore[arg-type]


@pytest.mark.parametrize(("mode", "routed"), [("on", True), ("shadow", False)])
async def test_the_runner_hands_the_cheaper_model_on_only_when_the_router_is_on(
    mode: str, routed: bool
) -> None:
    runner = StageRunner([_stage(SETTINGS, SMALL, LARGE)], _Modes(mode))
    ctx = _ctx(_req("hello"))
    (report,) = await runner.run(ctx, "pre_llm")
    assert report.applied
    assert report.shadow is not routed
    assert (ctx.price is SMALL) is routed
    assert (ctx.request.model == "mock-small") is routed


async def test_agreement_without_an_embedder_is_an_exact_match() -> None:
    verifier = Verifier(db=None, repo=None, embedder=None)  # type: ignore[arg-type]
    assert await verifier.agreement("Paris.", " Paris. ") == (1.0, 0)
    assert await verifier.agreement("Paris.", "Lyon.") == (0.0, 0)


class _Embedder:
    model = "e"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if t.startswith("Paris") else [0.6, 0.8] for t in texts]


async def test_agreement_is_the_cosine_of_the_answers() -> None:
    verifier = Verifier(db=None, repo=None, embedder=_Embedder())  # type: ignore[arg-type]
    score, embedded = await verifier.agreement("Paris.", "It is Lyon.")
    assert score == pytest.approx(0.6)
    assert embedded > 0
    assert (await verifier.agreement("", "Lyon"))[0] == 0.0  # nothing to embed: no match


def test_an_answer_with_only_tool_calls_is_compared_by_its_calls() -> None:
    calls = (ToolCall("c1", "lookup", '{"q": "x"}'),)
    response = CanonicalResponse("r", "m", None, calls, "tool_calls", Usage())
    assert said(response) == 'lookup({"q": "x"})'


def test_a_routed_answer_replays_with_the_model_that_gave_it() -> None:
    origin = Origin("req-1", datetime(2026, 11, 4, tzinfo=UTC), model="mock/mock-small")
    response = CanonicalResponse("r", "mock-small", "hi", (), "stop", Usage(1, 1))
    _, _, again = load_chat(dump_chat(origin, 5, response))
    assert again == Origin("req-1", origin.at, replayed=True, model="mock/mock-small")


def test_settings_are_validated_and_read_leniently() -> None:
    with pytest.raises(ValueError, match="two different models"):
        RoutingSettings(large_model="a/b", small_model="a/b")
    with pytest.raises(ValueError, match="two different models"):
        RoutingSettings(large_model="a/b")
    with pytest.raises(ValueError, match="tolerance"):
        RoutingSettings(tolerance=0.9)
    assert RoutingSettings.parse({"large_model": "a/b"}) == RoutingSettings()  # unusable
    assert RoutingSettings.parse({"threshold": "nan"}) == RoutingSettings()
    assert RoutingSettings.parse(SETTINGS.to_dict()) == SETTINGS
