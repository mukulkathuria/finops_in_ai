"""Graphify output (its `graph.json` of nodes and links) as a project's graph (US-22). Graphify
publishes no schema, so the node-link shape is read leniently. Nodes are taken by `id`, with a
type and a name or label; links by `source` and `target`, with a relation. A relation outside
the gateway's edge kinds, or a link to an unknown node, is counted and skipped. `EXTRACTED` edges
are certain; `INFERRED` ones keep their score."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final
from uuid import UUID, uuid7

from gateway.domain.code import MAX_NAME, MAX_PACK_CHARS, CodeGraph, Pack
from gateway.domain.graph import Edge, Vertex
from gateway.domain.parsing import text_tokens

_NODE_KINDS: Final = {
    "file": "file",
    "module": "module",
    "package": "module",
    "function": "symbol",
    "method": "symbol",
    "class": "symbol",
    "type": "symbol",
    "interface": "symbol",
    "struct": "symbol",
    "variable": "symbol",
    "constant": "symbol",
    "doc": "doc",
    "document": "doc",
    "concept": "doc",
    "section": "doc",
    "owner": "owner",
    "person": "owner",
    "team": "owner",
}
_EDGE_KINDS: Final = {
    "imports": "imports",
    "import": "imports",
    "calls": "calls",
    "call": "calls",
    "invokes": "calls",
    "contains": "defines",
    "defines": "defines",
    "declares": "defines",
    "documents": "documents",
    "describes": "documents",
    "owns": "owns",
}
_CONFIDENCE: Final = {"extracted": 1.0, "inferred": 0.6, "ambiguous": 0.3}


@dataclass(frozen=True, slots=True)
class GraphifyGraph:
    graph: CodeGraph
    skipped: int  # links of other relations, or to nodes the file does not have


def _text(value: object) -> str:
    return str(value) if value is not None else ""


def _confidence(link: Mapping[str, Any]) -> float:
    raw = link.get("confidence_score", link.get("confidence", link.get("weight")))
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return min(1.0, max(0.0, float(raw)))
    return _CONFIDENCE.get(_text(raw).lower(), 1.0)


def _packs(vertices: Iterable[Vertex], edges: Iterable[Edge]) -> tuple[Pack, ...]:
    """A pack per file or module: what it defines and imports (Graphify gives no signatures)."""
    by_id = {v.id: v for v in vertices}
    defines: dict[UUID, list[str]] = {}
    imports: dict[UUID, list[str]] = {}
    for e in edges:
        if e.kind == "defines":
            defines.setdefault(e.src, []).append(by_id[e.dst].name)
        elif e.kind == "imports":
            imports.setdefault(e.src, []).append(by_id[e.dst].name)
    packs = []
    for v in by_id.values():
        if v.kind not in ("module", "file") or not v.path:
            continue
        lines = [f"# {v.path} ({v.name})"]
        if v.id in imports:
            lines.append("imports: " + ", ".join(sorted(imports[v.id])))
        lines += [f"- {name}" for name in sorted(defines.get(v.id, []))]
        text = "\n".join(lines)[:MAX_PACK_CHARS]
        packs.append(Pack(v.id, v.path, text, text_tokens(text), 0))
    return tuple(packs)


def load_graphify(data: Mapping[str, Any]) -> GraphifyGraph:
    """The graph in a parsed `graph.json`; ValueError when it is not a node-link graph."""
    nodes = data.get("nodes")
    links = data.get("links", data.get("edges"))
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError("a graph needs `nodes` and `links` (or `edges`) lists")
    ids: dict[str, UUID] = {}
    vertices: list[Vertex] = []
    for node in nodes:
        key = _text(node.get("id")) if isinstance(node, Mapping) else ""
        if not key:
            raise ValueError("every node needs an id")
        if key in ids:
            continue
        kind = _text(node.get("type", node.get("kind", node.get("file_type")))).lower()
        name = (_text(node.get("label") or node.get("name")) or key)[:MAX_NAME]
        path = _text(node.get("source_file") or node.get("path") or node.get("file"))[:1_000]
        if path and name == PurePosixPath(path).name:  # named by its file: found by its stem
            name = PurePosixPath(path).stem or name
        vid = ids[key] = uuid7()
        vertices.append(Vertex(vid, _NODE_KINDS.get(kind, "symbol"), name, path or None))
    edges: dict[tuple[UUID, UUID, str], Edge] = {}
    skipped = 0
    for link in links:
        if not isinstance(link, Mapping):
            skipped += 1
            continue
        relation = _text(link.get("relation", link.get("type", link.get("label")))).lower()
        edge_kind = _EDGE_KINDS.get(relation)
        src, dst = ids.get(_text(link.get("source"))), ids.get(_text(link.get("target")))
        if edge_kind is None or src is None or dst is None:
            skipped += 1
            continue
        edges.setdefault((src, dst, edge_kind), Edge(src, dst, edge_kind, _confidence(link)))
    graph = CodeGraph(tuple(vertices), tuple(edges.values()), _packs(vertices, edges.values()))
    return GraphifyGraph(graph, skipped)
