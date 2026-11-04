"""Savings (US-17): the counterfactual adds back what applied stages removed (shadow runs removed
nothing), prices it at the baseline model without discounts, and the overhead of OCR, classifier
calls and embeddings is counted from the request's own record."""

from gateway.domain.llm import Usage
from gateway.domain.pricing import ModelPrice
from gateway.domain.savings import (
    OverheadRates,
    baseline_cost,
    file_conversion_report,
    original_input_tokens,
    overhead_cost,
    overhead_inputs,
)
from gateway.domain.stages import StageReport

SMALL = ModelPrice(1, "mock", "mock-small", "chat", price_input=150_000, price_output=600_000)
LARGE = ModelPrice(1, "mock", "mock-large", "chat", price_input=2_500_000, price_output=10_000_000)
APPLIED = StageReport("compression", "1.0", "pre_llm", 1_000, 600, -60, applied=True)
SHADOW = StageReport("compression", "1.0", "pre_llm", 1_000, 600, -60, applied=True, shadow=True)


def test_the_original_input_adds_back_what_applied_stages_removed() -> None:
    assert original_input_tokens(5_000, [APPLIED]) == 5_400
    assert original_input_tokens(5_000, [SHADOW]) == 5_000  # a shadow run sent nothing less
    assert original_input_tokens(5_000, []) == 5_000


def test_the_baseline_prices_the_original_request_at_the_baseline_model_in_full() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=900_000)
    assert baseline_cost(LARGE, usage, []) == 2_500_000 + 10_000_000  # no cached-token discount
    assert baseline_cost(SMALL, usage, [APPLIED]) == SMALL.cost(
        Usage(input_tokens=1_000_400, output_tokens=1_000_000)
    )


def test_overhead_counts_ocr_classifier_calls_and_embeddings() -> None:
    notes = {
        "files": [{"gpu_seconds": 0.5}, {"gpu_seconds": 0.25}, "not a report"],
        "retrieval": {"query_tokens": 250_000},
    }
    decisions = [
        {"check": "injection", "backend": "laya", "action": "passed"},
        {"check": "injection", "backend": "rules", "action": "passed"},  # free
        {"check": "injection", "backend": "jev", "action": "skipped"},  # never called
        {"check": "pii", "backend": "rules", "action": "redacted"},
    ]
    assert overhead_inputs(notes, decisions) == (0.75, 1, 250_000)
    assert overhead_inputs({}, []) == (0.0, 0, 0)
    rates = OverheadRates(ocr_gpu_second=300, classifier_call=20, embedding_per_mtok=10_000)
    assert overhead_cost(rates, gpu_seconds=0.75, classifier_calls=1, embedded_tokens=250_000) == (
        225 + 20 + 2_500
    )


def test_file_conversion_is_reported_as_a_stage() -> None:
    files = [{"tokens_before": 3_200, "tokens_after": 400, "kept_pages": [2]}]
    report = file_conversion_report(files, SMALL)
    assert report is not None
    assert (report.stage, report.tokens_before, report.tokens_after) == (
        "file_conversion",
        3_200,
        400,
    )
    assert report.applied
    assert report.cost_delta_micro_usd < 0
    assert report.decisions == {"files": 1, "kept_pages": 1}
    assert file_conversion_report(None, SMALL) is None
    assert file_conversion_report([], SMALL) is None
