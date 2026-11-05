"""The code and knowledge graph (spec section 5; US-22): what graph reads return, and the limits
every read and every indexed graph keep. Fixed-depth patterns go up to three hops, reachability
up to eight."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

Direction = Literal["out", "in"]
VERTEX_KINDS: Final = frozenset({"module", "file", "symbol", "doc", "owner"})
EDGE_KINDS: Final = frozenset({"imports", "calls", "defines", "documents", "owns"})
MAX_FIXED_DEPTH: Final = 3  # what one query of explicit joins answers
MAX_DEPTH: Final = 8  # reachability, by recursive CTE
MAX_WALK_IDS: Final = 20  # start vertices of a 2- or 3-hop walk (joins multiply paths)
MAX_RESULTS: Final = 1_000
MAX_GRAPH: Final = 200_000  # vertices, and edges, per project


@dataclass(frozen=True, slots=True)
class Vertex:
    id: UUID
    kind: str
    name: str
    path: str | None = None
    props: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Edge:
    src: UUID
    dst: UUID
    kind: str
    confidence: float = 1.0  # 1: read from the code; lower: inferred


@dataclass(frozen=True, slots=True)
class Reached:
    vertex: Vertex
    depth: int
    confidence: float | None = None  # the weakest edge on the best path (fixed-depth reads)


def check_read(kinds: Sequence[str], depth: int, limit: int, *, max_depth: int) -> None:
    """A graph read's parameters, or ValueError."""
    if not kinds or not set(kinds) <= EDGE_KINDS:
        raise ValueError(f"edge kinds are among {sorted(EDGE_KINDS)}")
    if not 1 <= depth <= max_depth:
        raise ValueError(f"depth is between 1 and {max_depth}")
    if not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit is between 1 and {MAX_RESULTS}")


def check_graph(vertices: Sequence[Vertex], edges: Sequence[Edge]) -> None:
    """A graph an indexer built: known kinds, edges between its own vertices, confidences in
    [0, 1], within the size limit. ValueError otherwise."""
    if len(vertices) > MAX_GRAPH or len(edges) > MAX_GRAPH:
        raise ValueError(f"a project's graph holds at most {MAX_GRAPH} vertices and edges")
    ids = {v.id for v in vertices}
    if len(ids) != len(vertices):
        raise ValueError("vertex ids repeat")
    if bad := next((v for v in vertices if v.kind not in VERTEX_KINDS), None):
        raise ValueError(f"unknown vertex kind {bad.kind!r}")
    for e in edges:
        if e.kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind {e.kind!r}")
        if e.src not in ids or e.dst not in ids:
            raise ValueError("an edge leaves the project's graph")
        if not 0.0 <= e.confidence <= 1.0:
            raise ValueError("edge confidence is between 0 and 1")
