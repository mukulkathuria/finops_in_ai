"""Graph retrieval for knowledge domains (ADR 0011): what a domain declares, what an extraction
returns, and the text rules the extractor and the query side share.

A domain (say `medical`) names its entity types (disease, symptom, test) and relation types
(has_symptom, diagnosed_by). Documents filed under it are read into relations between named
entities. A query names entities by the same normalized words."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

DOMAIN_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,62}$"
TYPE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,59}$"
MAX_RELATIONS_PER_CHUNK: Final = 50
MAX_NAME_CHARS: Final = 200
MAX_NGRAM: Final = 4  # the longest entity name a query is matched on, in words
MAX_QUERY_TERMS: Final = 500
MAX_CANDIDATES: Final = 5  # entities the graph answer centres on
DEFAULT_INTENT_THRESHOLD: Final = 0.5  # cosine to the domain's example questions

_SPACE = re.compile(r"\s+")
_ENDS = re.compile(r"^[\W_]+|[\W_]+$")
_WORD = re.compile(r"\w[\w'-]*")
_TYPE = re.compile(TYPE_PATTERN)


class ExtractionFailed(Exception):
    """The extraction model could not be reached, or answered unreadably."""


@dataclass(frozen=True, slots=True)
class DomainSchema:
    name: str
    description: str
    entity_types: tuple[str, ...]
    relation_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Mention:
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Relation:
    source: Mention
    type: str
    target: Mention


@dataclass(frozen=True, slots=True)
class Extraction:
    relations: tuple[Relation, ...]
    tokens: int  # what the extraction model was sent and returned


def graph_norm(name: str) -> str:
    """The form entity names are matched in: lower case, single spaces, no punctuation at the
    ends."""
    return _ENDS.sub("", _SPACE.sub(" ", name.strip().lower()))


def query_terms(query: str) -> list[str]:
    """Every run of 1 to MAX_NGRAM words of a query, normalized: the names it could mention."""
    words = [w for w in (graph_norm(m) for m in _WORD.findall(query.lower())) if w]
    terms: dict[str, None] = {}
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(words) - n + 1):
            terms[" ".join(words[i : i + n])] = None
            if len(terms) >= MAX_QUERY_TERMS:
                return list(terms)
    return list(terms)


def check_types(types: Sequence[str], what: str) -> tuple[str, ...]:
    """Lower-case snake_case names, each once, or ValueError."""
    cleaned = tuple(dict.fromkeys(t.strip().lower() for t in types))
    if not cleaned or any(not _TYPE.match(t) for t in cleaned):
        raise ValueError(f"{what} are lower-case words joined by underscores")
    return cleaned


def clean(relations: Sequence[Relation], schema: DomainSchema) -> tuple[Relation, ...]:
    """The relations of the domain's types between named entities of its types, normalized and
    each once; at most MAX_RELATIONS_PER_CHUNK. A model's other output is dropped, not trusted."""
    entity_types = set(schema.entity_types)
    relation_types = set(schema.relation_types)
    kept: dict[tuple[str, str, str, str, str], Relation] = {}
    for r in relations:
        rtype = r.type.strip().lower()
        ends = [
            Mention(_SPACE.sub(" ", m.name.strip())[:MAX_NAME_CHARS], m.type.strip().lower())
            for m in (r.source, r.target)
        ]
        if rtype not in relation_types or any(
            m.type not in entity_types or not graph_norm(m.name) for m in ends
        ):
            continue
        source, target = ends
        key = (source.type, graph_norm(source.name), rtype, target.type, graph_norm(target.name))
        if key[1] == key[4] and key[0] == key[3]:
            continue  # an entity related to itself says nothing
        kept.setdefault(key, Relation(source, rtype, target))
        if len(kept) >= MAX_RELATIONS_PER_CHUNK:
            break
    return tuple(kept.values())


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    """The unit-length mean of the example questions' embeddings: the domain's intent."""
    if not vectors:
        raise ValueError("a domain needs at least one example question")
    mean = [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]
    norm = math.sqrt(sum(x * x for x in mean))
    if norm == 0:
        raise ValueError("the example questions have no direction")
    return [x / norm for x in mean]
