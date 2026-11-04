"""US-22 eval gate: the context packs for the fixed questions about this repository hold the
expected modules and symbols, at a fraction of the whole modules' tokens."""

from pathlib import Path

from scripts.eval_code_context import GATE, evaluate, hit_rate, report

REPOSITORY = Path(__file__).resolve().parents[2]


def test_the_eval_questions_hit_with_far_fewer_tokens_than_whole_modules() -> None:
    results = evaluate(REPOSITORY)
    assert hit_rate(results) >= GATE, report(results)
    assert sum(r.tokens for r in results) < sum(r.source_tokens for r in results) / 3
    assert "hit rate" in report(results)
