"""Savings against a counterfactual (spec section 4, rule 3; US-17).

For each request the baseline is what it would have cost as the client sent it: its input before
any optimization (what each applied stage removed is added back; a shadow run removed nothing)
and the output it got, priced at the org's baseline model without discounts such as cached-token
prices. The actual cost is what the provider charged plus the overhead of the gateway's own work:
OCR compute, classifier calls and embeddings. Savings are the baseline minus the actual cost, and
can be negative.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from gateway.domain.llm import Usage
from gateway.domain.pricing import ModelPrice
from gateway.domain.retrieval import RAG_RELEVANCE, RAG_RELEVANCE_VERSION
from gateway.domain.stages import StageReport

_CLASSIFIER_CALLED: Final = frozenset({"passed", "flagged", "blocked"})


@dataclass(frozen=True, slots=True)
class OverheadRates:
    """What the gateway's own work costs, in micro-USD (configured: it runs on your hardware)."""

    ocr_gpu_second: int
    classifier_call: int
    embedding_per_mtok: int


def original_input_tokens(input_tokens: int, reports: Sequence[StageReport]) -> int:
    """The input the request would have had without the stages that ran for real."""
    removed = sum(r.tokens_before - r.tokens_after for r in reports if r.applied and not r.shadow)
    return max(0, input_tokens + removed)


def baseline_cost(price: ModelPrice, usage: Usage, reports: Sequence[StageReport]) -> int:
    """The counterfactual cost: the original input and the actual output, at full price."""
    original = original_input_tokens(usage.input_tokens, reports)
    return price.cost(Usage(input_tokens=original, output_tokens=usage.output_tokens))


def overhead_inputs(
    notes: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> tuple[float, int, int]:
    """(OCR GPU seconds, classifier calls, embedded tokens) of one request, from its ledger notes
    and guardrail decisions. A file read from the parse cache cost nothing this time."""
    files = notes.get("files")
    gpu = 0.0
    if isinstance(files, list):
        gpu = sum(float(f.get("gpu_seconds") or 0.0) for f in files if isinstance(f, Mapping))
    calls = sum(
        1
        for d in decisions
        if d.get("check") == "injection"
        and d.get("backend") != "rules"
        and d.get("action") in _CLASSIFIER_CALLED
    )
    retrieval = notes.get("retrieval")
    relevance = retrieval.get("relevance") if isinstance(retrieval, Mapping) else None
    if isinstance(relevance, Mapping) and relevance.get("rerank") == "ok":
        calls += 1  # a rerank costs about what a classifier call does
    embedded = 0
    for step in ("retrieval", "cache", "judge"):  # embedded to retrieve, match or compare
        record = notes.get(step)
        if isinstance(record, Mapping):
            embedded += int(record.get("query_tokens") or 0)
    return gpu, calls, embedded


def overhead_cost(
    rates: OverheadRates, *, gpu_seconds: float, classifier_calls: int, embedded_tokens: int
) -> int:
    return (
        math.ceil(gpu_seconds * rates.ocr_gpu_second)
        + classifier_calls * rates.classifier_call
        + math.ceil(embedded_tokens * rates.embedding_per_mtok / 1_000_000)
    )


def file_conversion_report(files: object, price: ModelPrice) -> StageReport | None:
    """Attachments sent as Markdown instead of pages and pictures, reported as a stage."""
    if not isinstance(files, list):
        return None
    reports = [f for f in files if isinstance(f, Mapping)]
    if not reports:
        return None
    before = sum(int(f.get("tokens_before") or 0) for f in reports)
    after = sum(int(f.get("tokens_after") or 0) for f in reports)
    kept = sum(len(f.get("kept_pages") or ()) for f in reports)
    return StageReport(
        "file_conversion",
        "1",
        "pre_llm",
        before,
        after,
        price.cost(Usage(input_tokens=after)) - price.cost(Usage(input_tokens=before)),
        quality_risk=0.1,
        applied=before != after,
        decisions={"files": len(reports), "kept_pages": kept},
    )


def provider_cache_report(price: ModelPrice, usage: Usage) -> StageReport | None:
    """The provider's prompt cache, reported as a stage once the request is priced: what reads
    at the cached price saved and writes at the write premium cost, against full price."""
    if not (usage.cached_tokens or usage.cache_write_tokens):
        return None
    full = Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    return StageReport(
        "provider_cache",
        "1",
        "post_llm",
        0,
        0,
        price.cost(usage) - price.cost(full),
        applied=True,
        decisions={"read": usage.cached_tokens, "written": usage.cache_write_tokens},
    )


def cache_hit_report(
    kind: str, usage: Usage, similarity: float | None, price: ModelPrice
) -> StageReport:
    """An answer served from the response cache saves all it would cost at full price, at the
    model that would answer, so the techniques add up to the baseline whatever the answer cost
    when it was made (a provider's cache discount included)."""
    full = Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    return StageReport(
        f"{kind}_cache",
        "1",
        "pre_llm",
        usage.input_tokens + usage.output_tokens,
        0,
        -price.cost(full),
        applied=True,
        decisions={"similarity": round(similarity, 4)} if similarity is not None else {},
    )


def unserved(reports: Sequence[StageReport]) -> list[StageReport]:
    """The reports of a request that was not served (an error, a cancellation, a block): what
    the stages decided is kept, but none counts as applied or as saving anything."""
    return [replace(r, cost_delta_micro_usd=0, applied=False) for r in reports]


def code_context_report(code: object, price: ModelPrice) -> StageReport | None:
    """US-22: context packs sent in place of the whole modules they outline, as a stage report
    from the request's code-context notes."""
    if not isinstance(code, Mapping):
        return None
    before = int(code.get("source_tokens") or 0)
    after = int(code.get("tokens") or 0)
    return StageReport(
        "code_context",
        "1",
        "pre_llm",
        before,
        after,
        price.cost(Usage(input_tokens=after)) - price.cost(Usage(input_tokens=before)),
        quality_risk=0.1,
        applied=before != after,
        decisions={
            "modules": len(code.get("modules") or ()),
            "related": int(code.get("related") or 0),
            "dropped": len(code.get("dropped") or ()),
        },
    )


def relevance_report(retrieval: object, price: ModelPrice) -> StageReport | None:
    """US-21: the relevance filter as a stage report, from the request's retrieval notes: the k
    nearest passages against those kept (in shadow, those that would have been)."""
    relevance = retrieval.get("relevance") if isinstance(retrieval, Mapping) else None
    if not isinstance(relevance, Mapping):
        return None
    shadow = relevance.get("mode") == "shadow"
    if relevance.get("rerank") != "ok":
        return StageReport(
            RAG_RELEVANCE,
            RAG_RELEVANCE_VERSION,
            "pre_llm",
            0,
            0,
            0,
            shadow=shadow,
            decisions={"rerank": "failed"},
        )
    before = int(relevance.get("tokens_before") or 0)
    after = int(relevance.get("tokens_after") or 0)
    delta = price.cost(Usage(input_tokens=after)) - price.cost(Usage(input_tokens=before))
    return StageReport(
        RAG_RELEVANCE,
        RAG_RELEVANCE_VERSION,
        "pre_llm",
        before,
        after,
        delta,
        quality_risk=0.1,
        applied=before != after,
        shadow=shadow,
        decisions={
            "kept": int(relevance.get("kept") or 0),
            "dropped": int(relevance.get("dropped") or 0),
        },
    )
