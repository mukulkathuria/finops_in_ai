"""US-21: passages under the threshold are dropped, the rest packed within the token budget by
score; the reranker answer is checked one to one; the report prices what was pruned."""

from uuid import uuid4

import httpx2
import pytest

from gateway.adapters.rerank.http import HttpReranker, RerankError, scores_from
from gateway.domain.pricing import ModelPrice
from gateway.domain.retrieval import Hit, RelevanceSettings, pack
from gateway.domain.savings import relevance_report
from gateway.domain.stages import StageSetting
from gateway.pipeline.stages import RagRelevance, StageRunner

PRICE = ModelPrice(1, "mock", "m", "chat", 1_000_000, 4_000_000)


def _hit(tokens: int, content: str = "x") -> Hit:
    return Hit(uuid4(), uuid4(), "doc", content, tokens, 0.5)


def test_passages_under_the_threshold_are_dropped_and_the_rest_packed_by_score() -> None:
    near, mid, far, tiny = _hit(100), _hit(300), _hit(50), _hit(10)
    packed = pack([near, mid, far, tiny], [0.9, 0.8, 0.1, 0.7], threshold=0.3, max_tokens=150, k=3)
    assert [h.chunk_id for h in packed.kept] == [near.chunk_id, tiny.chunk_id]  # mid is too big
    assert [h.score for h in packed.kept] == [0.9, 0.7]  # the reranker's scores are kept
    assert (packed.tokens_before, packed.tokens_after, packed.dropped) == (450, 110, 2)
    assert pack([], [], threshold=0.3, max_tokens=100, k=5).kept == ()
    assert pack([near], [0.1], threshold=0.3, max_tokens=1_000, k=5).kept == ()
    assert len(pack([tiny, tiny, tiny], [0.9] * 3, threshold=0, max_tokens=1_000, k=2).kept) == 2


def test_a_reranker_answer_maps_one_to_one() -> None:
    raw = (
        b'{"results": [{"index": 1, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.9}]}'
    )
    assert scores_from(raw, 2) == [0.9, 0.2]
    for bad in (
        b'{"results": [{"index": 0, "relevance_score": 0.9}]}',  # one left unscored
        b'{"results": [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 1}]}',
        b'{"results": [{"index": 5, "relevance_score": 1}, {"index": 0, "relevance_score": 1}]}',
        b'{"results": [{"index": 0, "relevance_score": "NaN"}]}',
        b"not json",
    ):
        with pytest.raises(RerankError):
            scores_from(bad, 2)


async def test_the_http_reranker_posts_the_cohere_shape_and_fails_typed() -> None:
    seen: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"results": [{"index": 0, "relevance_score": 0.4}]})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(answer))
    reranker = HttpReranker("http://rerank.test/v1/", "bge", http=http, api_key="k")
    assert await reranker.rerank("q", ["p"]) == [0.4]
    assert str(seen[0].url) == "http://rerank.test/v1/rerank"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert await reranker.rerank("q", []) == []  # nothing to score: no call
    down = HttpReranker(
        "http://rerank.test/v1",
        "bge",
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(503))),
    )
    with pytest.raises(RerankError, match="503"):
        await down.rerank("q", ["p"])


def test_the_report_prices_what_was_pruned_and_tells_shadow_from_on() -> None:
    notes = {
        "relevance": {
            "mode": "on",
            "rerank": "ok",
            "tokens_before": 1_000,
            "tokens_after": 400,
            "kept": 1,
            "dropped": 3,
        }
    }
    report = relevance_report(notes, PRICE)
    assert report is not None
    assert (report.stage, report.applied, report.shadow) == ("rag_relevance", True, False)
    assert report.cost_delta_micro_usd == -600  # 600 input tokens at 1.00 per million
    shadow = relevance_report({"relevance": {**notes["relevance"], "mode": "shadow"}}, PRICE)
    assert shadow is not None
    assert shadow.shadow
    failed = relevance_report({"relevance": {"mode": "on", "rerank": "failed"}}, PRICE)
    assert failed is not None
    assert (failed.applied, failed.decisions) == (False, {"rerank": "failed"})
    assert relevance_report({}, PRICE) is None


def test_settings_are_validated_and_read_leniently() -> None:
    with pytest.raises(ValueError, match="threshold"):
        RelevanceSettings(threshold=2)
    assert RelevanceSettings.parse({"threshold": "x"}) == RelevanceSettings()
    custom = RelevanceSettings(0.5, 2_000, 10)
    assert RelevanceSettings.parse(custom.to_dict()) == custom


class _Modes:
    def __init__(self, settings: dict[str, StageSetting]) -> None:
        self.settings = settings

    async def get(self, org_id: object) -> dict[str, StageSetting]:
        return self.settings


async def test_the_filter_is_listed_and_gated_like_a_stage_but_runs_in_retrieval() -> None:
    runner = StageRunner([], _Modes({}), descriptors=[RagRelevance()])
    assert [s.name for s in runner.stages] == ["rag_relevance"]
    assert await runner.mode(uuid4(), "rag_relevance") == "off"  # ships off
    assert await runner.mode(uuid4(), "nope") == "off"
    on = StageRunner(
        [], _Modes({"rag_relevance": StageSetting("on", "1.0")}), descriptors=[RagRelevance()]
    )
    assert await on.mode(uuid4(), "rag_relevance") == "on"
