"""US-22 eval: the context-pack hit rate and token use on fixed questions about this repository.

    python scripts/eval_code_context.py [repository]

The repository is read by the Python indexer, as `POST /v1/projects/{id}/index` reads a source
bundle, and each question gets the packs `GET /v1/projects/{id}/context` would choose: vertices
the question names (the last part of a dotted name), ranked, packed within 4,000 tokens; the
signatures one call away are left out. A question hits when its context holds the expected module
and symbol. Exits 1 under the 0.9 hit-rate gate."""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gateway.adapters.code.python_ast import outline
from gateway.domain.code import (
    FIND_KINDS,
    FOUND_LIMIT,
    MAX_PACKS,
    CodeGraph,
    build_graph,
    choose,
    question_terms,
    rank,
    term_of,
)
from gateway.domain.graph import Vertex
from scripts.pack_sources import bundle

BUDGET: Final = 4_000
GATE: Final = 0.9


@dataclass(frozen=True, slots=True)
class Question:
    text: str
    module: str  # the module its context must hold
    symbol: str  # and the symbol the question names


QUESTIONS: Final = (
    Question(
        "How does ChatService settle a budget hold when a stream fails?",
        "gateway/pipeline/chat.py",
        "ChatService",
    ),
    Question(
        "How does PostgresGraphStore walk reachable vertices?",
        "gateway/adapters/postgres/graph.py",
        "PostgresGraphStore",
    ),
    Question(
        "How does share_groups decide who a document is shared with?",
        "gateway/domain/retrieval.py",
        "share_groups",
    ),
    Question(
        "What does load_graphify do with relations it does not know?",
        "gateway/adapters/code/graphify.py",
        "load_graphify",
    ),
    Question(
        "How does the RouterStage choose between the small and the large tier?",
        "gateway/pipeline/routing.py",
        "RouterStage",
    ),
    Question(
        "How does the CacheJanitor purge expired cache entries?",
        "worker/jobs/cache_janitor.py",
        "CacheJanitor",
    ),
    Question(
        "Which price changes does plan_sync refuse?", "gateway/domain/price_list.py", "plan_sync"
    ),
    Question(
        "How does the StageRunner decide a stage's mode?",
        "gateway/pipeline/stages.py",
        "StageRunner",
    ),
    Question(
        "How does text_tokens estimate a token count?", "gateway/domain/parsing.py", "text_tokens"
    ),
    Question(
        "How does the Envelope seal data with a wrapped key?",
        "gateway/adapters/security/envelope.py",
        "Envelope",
    ),
    Question(
        "When does the CircuitBreaker open?", "gateway/adapters/resilience.py", "CircuitBreaker"
    ),
    Question(
        "How does compact shrink the spaces in a prompt?",
        "gateway/domain/compression.py",
        "compact",
    ),
)


@dataclass(frozen=True, slots=True)
class Result:
    question: Question
    hit: bool
    modules: tuple[str, ...]
    tokens: int  # the packs sent
    source_tokens: int  # the whole modules


def find(graph: CodeGraph, terms: frozenset[str]) -> list[Vertex]:
    """`GraphStore.find` in memory: the same match, order and limit as its SQL."""
    found = [v for v in graph.vertices if v.kind in FIND_KINDS and term_of(v.name) in terms]
    return sorted(found, key=lambda v: (v.kind, v.name))[:FOUND_LIMIT]


def index(root: Path) -> CodeGraph:
    outlines = (outline(path, source) for path, source in bundle(root).items())
    return build_graph([m for m in outlines if m is not None])


def ask(graph: CodeGraph, question: Question) -> Result:
    found = find(graph, question_terms(question.text))
    chosen = choose(graph.packs, rank(found)[:MAX_PACKS], BUDGET)
    paths = {p.path for p in chosen}
    symbols = {v.name for v in found if v.kind == "symbol" and v.path in paths}
    return Result(
        question,
        question.module in paths and question.symbol in symbols,
        tuple(p.path for p in chosen),
        sum(p.tokens for p in chosen),
        sum(p.source_tokens for p in chosen),
    )


def evaluate(root: Path) -> list[Result]:
    graph = index(root)
    return [ask(graph, q) for q in QUESTIONS]


def hit_rate(results: Sequence[Result]) -> float:
    return sum(r.hit for r in results) / len(results)


def report(results: Sequence[Result]) -> str:
    tokens = sum(r.tokens for r in results)
    source = sum(r.source_tokens for r in results)
    lines = [
        f"{'hit ' if r.hit else 'MISS'} {r.tokens:>6} / {r.source_tokens:>6} tokens"
        f"  {r.question.text}"
        for r in results
    ]
    lines.append(
        f"hit rate {hit_rate(results):.2f} ({sum(r.hit for r in results)} of {len(results)});"
        f" context {tokens} tokens against {source} for the whole modules"
        f" ({tokens / max(source, 1):.1%})"
    )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    results = evaluate(root)
    print(report(results))
    return 0 if hit_rate(results) >= GATE else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
