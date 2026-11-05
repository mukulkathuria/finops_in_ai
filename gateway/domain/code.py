"""Code context (spec section 4, F5; US-22). A project's modules and symbols become a graph, and
each module a context pack: its path, docstring, imports and importers and its signatures, so a
model can reason about it without the whole source, at a fraction of the tokens."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID, uuid7

from gateway.domain.graph import Edge, Vertex
from gateway.domain.parsing import text_tokens

INFERRED: Final = 0.6  # a call resolved by a name unique in the project, not by what is in scope
MAX_PACK_CHARS: Final = 20_000  # a pack is a module's outline, never its source
MAX_NAME: Final = 500  # a vertex name's limit in the database
MAX_MODULES: Final = 20_000
MAX_MODULE_CHARS: Final = 512_000  # a bigger file (generated code, data) is left out
FIND_KINDS: Final = ("module", "file", "symbol")  # what a question's terms match
FOUND_LIMIT: Final = 200  # vertices one question may match
MAX_PACKS: Final = 40  # modules considered for one context
_WORD: Final = re.compile(r"[^\W\d]\w+")  # an identifier, in any script
_CAMEL: Final = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]+")
_STOP: Final = frozenset(
    {
        "about",
        "and",
        "are",
        "can",
        "class",
        "code",
        "does",
        "file",
        "for",
        "from",
        "function",
        "get",
        "has",
        "have",
        "how",
        "into",
        "its",
        "method",
        "module",
        "not",
        "set",
        "that",
        "the",
        "their",
        "there",
        "this",
        "use",
        "used",
        "uses",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str  # within its module: `Class.method`
    kind: str  # class | function
    signature: str
    doc: str = ""  # the first line of its docstring
    calls: tuple[str, ...] = ()  # the simple names it calls


@dataclass(frozen=True, slots=True)
class Module:
    path: str  # posix, within the project
    name: str  # dotted
    doc: str = ""
    imports: tuple[str, ...] = ()  # absolute dotted names
    symbols: tuple[Symbol, ...] = ()
    source_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Pack:
    module_id: UUID
    path: str
    text: str
    tokens: int
    source_tokens: int  # the whole module's: what sending the file would cost


@dataclass(frozen=True, slots=True)
class CodeGraph:
    vertices: tuple[Vertex, ...]
    edges: tuple[Edge, ...]
    packs: tuple[Pack, ...]


def one_line(text: str, limit: int) -> str:
    """Text safe to store and to show on one line: control characters and line breaks become a
    space, and a lone surrogate (a docstring's escapes can make one) a question mark."""
    single = _CONTROL.sub(" ", text).strip()
    return single.encode("utf-8", "replace").decode("utf-8")[:limit]


def first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return one_line(lines[0], 300) if lines else ""


def clean_path(raw: str) -> str | None:
    """A relative posix path inside the project, or None: absolute, climbing out, empty, too long,
    or holding control characters or anything UTF-8 cannot carry."""
    if len(raw) > MAX_NAME or _CONTROL.search(raw):
        return None
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        return None
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    return path.as_posix()


def module_name(path: str) -> str:
    """`gateway/pipeline/chat.py` is `gateway.pipeline.chat`; a package's `__init__.py` is the
    package."""
    parts = [p for p in path.removesuffix(".py").split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__main__"


def _resolve(imported: str, modules: Mapping[str, Module]) -> str | None:
    """The project module an import names: itself, or the module a name is imported from."""
    name = imported
    while name:
        if name in modules:
            return name
        name = name.rpartition(".")[0]
    return None


def cut(text: str, max_chars: int) -> str:
    """The text cut at a line end to fit `max_chars`, saying how many lines it left out; "" when
    not even the first line fits."""
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 + len(f"- ... {len(lines) - i} more lines") > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join([*kept, f"- ... {len(lines) - len(kept)} more lines"]) if kept else ""


def fit(pack: Pack, budget: int) -> Pack | None:
    """The pack within `budget` tokens: whole, cut at a line end, or None when not even its first
    line fits."""
    if pack.tokens <= budget:
        return pack
    text = cut(pack.text, budget * 4 - 3)  # text_tokens counts four characters a token
    return replace(pack, text=text, tokens=text_tokens(text)) if text else None


def render_pack(module: Module, imports: Sequence[str], importers: Sequence[str]) -> str:
    lines = [f"# {module.path} ({module.name})"]
    if module.doc:
        lines.append(module.doc)
    if imports:
        lines.append("imports: " + ", ".join(imports))
    if importers:
        shown = sorted(importers)
        more = f" and {len(shown) - 30} more" if len(shown) > 30 else ""
        lines.append("imported by: " + ", ".join(shown[:30]) + more)
    lines += [f"- {s.signature}" + (f"  # {s.doc}" if s.doc else "") for s in module.symbols]
    return cut("\n".join(lines), MAX_PACK_CHARS)


def build_graph(modules: Sequence[Module]) -> CodeGraph:
    """Vertices for the modules and their symbols. Edges: imports between the project's modules,
    each module defining its symbols, and calls (certain inside a module, inferred across modules
    when the name is unique in the project). One pack per module."""
    if len(modules) > MAX_MODULES:
        raise ValueError(f"a project indexes at most {MAX_MODULES} modules")
    by_name = {m.name: m for m in modules}
    module_ids = {name: uuid7() for name in by_name}
    vertices = [
        Vertex(module_ids[m.name], "module", m.name, m.path, {"doc": m.doc})
        for m in by_name.values()
    ]
    edges: dict[tuple[UUID, UUID, str], Edge] = {}
    symbol_ids: dict[tuple[str, str], UUID] = {}
    by_simple: dict[str, list[UUID]] = {}
    for m in by_name.values():
        for s in m.symbols:
            if (m.name, s.name) in symbol_ids:
                continue  # redefined: the first definition stays
            sid = symbol_ids[(m.name, s.name)] = uuid7()
            props = {"kind": s.kind, "signature": s.signature}
            vertices.append(Vertex(sid, "symbol", s.name, m.path, props))
            edges[(module_ids[m.name], sid, "defines")] = Edge(module_ids[m.name], sid, "defines")
            by_simple.setdefault(s.name.rpartition(".")[2], []).append(sid)
    imports: dict[str, list[str]] = {}
    importers: dict[str, list[str]] = {}
    for m in by_name.values():
        targets = {t for i in m.imports if (t := _resolve(i, by_name)) is not None}
        for target in sorted(targets - {m.name}):
            src, dst = module_ids[m.name], module_ids[target]
            edges[(src, dst, "imports")] = Edge(src, dst, "imports")
            imports.setdefault(m.name, []).append(target)
            importers.setdefault(target, []).append(m.name)
    for m in by_name.values():
        local: dict[str, UUID] = {}
        for s in m.symbols:
            local.setdefault(s.name.rpartition(".")[2], symbol_ids[(m.name, s.name)])
        for s in m.symbols:
            src = symbol_ids[(m.name, s.name)]
            for callee in sorted(set(s.calls)):
                target_id, confidence = local.get(callee), 1.0
                if target_id is None:
                    found = by_simple.get(callee, [])
                    target_id, confidence = (found[0] if len(found) == 1 else None), INFERRED
                if target_id is not None and target_id != src:
                    edge = Edge(src, target_id, "calls", confidence)
                    edges.setdefault((src, target_id, "calls"), edge)
    packs = []
    for m in by_name.values():
        text = render_pack(m, imports.get(m.name, []), importers.get(m.name, []))
        packs.append(Pack(module_ids[m.name], m.path, text, text_tokens(text), m.source_tokens))
    return CodeGraph(tuple(vertices), tuple(edges.values()), tuple(packs))


def term_of(name: str) -> str:
    """The term a vertex is found by: the last part of its dotted name, lower-cased (what the
    `graph_vertex_term` index holds)."""
    return name.rpartition(".")[2].lower()


def _identifiers(question: str) -> list[str]:
    return _WORD.findall(question[:4_000])


def _useful(term: str) -> bool:
    return len(term) > 2 and term not in _STOP


def question_words(question: str) -> frozenset[str]:
    """The whole identifiers a question names, lower-cased: a vertex named one of them is named by
    the question itself, not by a part of a longer name."""
    return frozenset(w for w in (i.lower() for i in _identifiers(question)) if _useful(w))


def question_terms(question: str) -> frozenset[str]:
    """The identifiers a question names, lower-cased, and their CamelCase and snake_case parts:
    `ChatService.settle_hold` gives chatservice, chat, service, settle_hold, settle and hold."""
    terms: set[str] = set()
    for word in _identifiers(question):
        terms.add(word.lower())
        terms.update(part.lower() for part in word.split("_") if part)
        terms.update(part.lower() for part in _CAMEL.findall(word))
    return frozenset(t for t in terms if _useful(t))


def rank(found: Sequence[Vertex], words: frozenset[str] = frozenset()) -> list[str]:
    """Module paths by how much of a question they hold: a module named in it counts 3, each of
    its symbols 2, and 2 more for a name the question gives whole rather than by a part; ties go
    to the shorter path."""
    scores: dict[str, int] = {}
    for v in found:
        if v.path:
            score = 3 if v.kind in ("module", "file") else 2
            if term_of(v.name) in words:
                score += 2
            scores[v.path] = scores.get(v.path, 0) + score
    return sorted(scores, key=lambda p: (-scores[p], len(p), p))


def choose(packs: Sequence[Pack], order: Sequence[str], max_tokens: int) -> list[Pack]:
    """The packs of the best-ranked modules that fit the budget (a smaller one may still fit
    after a bigger one did not). The best-ranked module is never left out for its size: its pack
    is cut to the budget instead."""
    by_path: dict[str, list[Pack]] = {}
    for p in packs:
        by_path.setdefault(p.path, []).append(p)
    chosen: list[Pack] = []
    used = 0
    for path in order:
        for p in by_path.get(path, []):
            if used + p.tokens <= max_tokens:
                chosen.append(p)
                used += p.tokens
            elif not chosen and (cut_pack := fit(p, max_tokens - used)) is not None:
                chosen.append(cut_pack)
                used += cut_pack.tokens
    return chosen
