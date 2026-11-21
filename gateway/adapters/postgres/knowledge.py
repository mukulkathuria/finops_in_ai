"""Knowledge domains and their graphs (migration 0033, ADR 0011).

Reads run as `app` under row security. Each one also asks that a relation have evidence in a
document the caller may read: the graph never shows a relation only another team's documents
state. The worker's writes run as `worker`, across orgs, one document at a time."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.domain.knowledge import DomainSchema, Relation, graph_norm
from gateway.domain.retrieval import Hit


@dataclass(frozen=True, slots=True)
class DomainRecord:
    id: UUID
    name: str
    description: str
    entity_types: tuple[str, ...]
    relation_types: tuple[str, ...]
    examples: tuple[str, ...]
    intent_threshold: float
    embedding_model: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Intent:
    domain: DomainRecord
    score: float  # cosine of the query to the domain's example questions


@dataclass(frozen=True, slots=True)
class Entity:
    id: UUID
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Candidate:
    entity: Entity
    matched: int  # how many of the query's entities it is related to


@dataclass(frozen=True, slots=True)
class Claim:
    document_id: UUID
    org_id: UUID
    domain_id: UUID | None


_CREATE: Final = """
INSERT INTO knowledge_domains (org_id, name, description, entity_types, relation_types,
                               examples, intent_threshold, intent_embedding, embedding_model,
                               created_by)
VALUES (app_current_org(), :name, :description, CAST(:entity_types AS text[]),
        CAST(:relation_types AS text[]), CAST(:examples AS text[]), :threshold,
        CAST(:vector AS vector), :model, :created_by)
ON CONFLICT (org_id, name) DO NOTHING
RETURNING id, name, description, entity_types, relation_types, examples, intent_threshold,
          embedding_model, created_at
"""
_LIST: Final = """
SELECT id, name, description, entity_types, relation_types, examples, intent_threshold,
       embedding_model, created_at
FROM knowledge_domains ORDER BY name
"""
_BY_NAME: Final = """
SELECT id, name, description, entity_types, relation_types, examples, intent_threshold,
       embedding_model, created_at
FROM knowledge_domains WHERE name = :name
"""
_DELETE: Final = "DELETE FROM knowledge_domains WHERE id = :id RETURNING id"
_INTENT: Final = """
SELECT id, name, description, entity_types, relation_types, examples, intent_threshold,
       embedding_model, created_at, 1 - (intent_embedding <=> CAST(:query AS vector)) AS score
FROM knowledge_domains
WHERE embedding_model = :model
ORDER BY intent_embedding <=> CAST(:query AS vector), id
LIMIT 1
"""
# An entity counts only through relations stated in documents the caller may read.
_LINKED: Final = """
SELECT e.id, e.name, e.type
FROM kg_entities e
WHERE e.domain_id = :domain AND e.norm = ANY(CAST(:terms AS text[]))
  AND EXISTS (
    SELECT 1 FROM kg_relations r
    JOIN kg_evidence v ON v.relation_id = r.id
    JOIN documents d ON d.id = v.document_id
    WHERE (r.src = e.id OR r.dst = e.id) AND d.acl_groups && CAST(:groups AS text[]))
ORDER BY e.name, e.id
LIMIT 50
"""
# The entities one relation from the query's, by how many of the query's they are related to.
_CANDIDATES: Final = """
WITH named AS (SELECT unnest(CAST(:entities AS uuid[])) AS id),
touching AS (
  SELECT r.id, r.src AS near, r.dst AS far FROM named n JOIN kg_relations r ON r.src = n.id
  UNION ALL
  SELECT r.id, r.dst, r.src FROM named n JOIN kg_relations r ON r.dst = n.id
)
SELECT e.id, e.name, e.type, count(DISTINCT t.near) AS matched
FROM touching t
JOIN kg_entities e ON e.id = t.far
WHERE t.far NOT IN (SELECT id FROM named)
  AND EXISTS (SELECT 1 FROM kg_evidence v JOIN documents d ON d.id = v.document_id
              WHERE v.relation_id = t.id AND d.acl_groups && CAST(:groups AS text[]))
GROUP BY e.id, e.name, e.type
ORDER BY matched DESC, e.name, e.id
LIMIT :top
"""
# The chunks that state the relations of the answer's centre, best centre first. Each chunk is
# read through the same ACL as a vector search.
_EVIDENCE: Final = """
WITH centre AS (
  SELECT * FROM unnest(CAST(:centre AS uuid[]), CAST(:weights AS float8[])) AS c(id, weight)
), rel AS (
  SELECT id, max(weight) AS weight FROM (
    SELECT r.id, c.weight FROM centre c JOIN kg_relations r ON r.src = c.id
    UNION ALL
    SELECT r.id, c.weight FROM centre c JOIN kg_relations r ON r.dst = c.id
  ) x
  GROUP BY id
), ev AS (
  SELECT v.chunk_id, max(rel.weight) AS weight, count(*) AS support
  FROM rel JOIN kg_evidence v ON v.relation_id = rel.id
  GROUP BY v.chunk_id
)
SELECT ch.id AS chunk_id, ch.document_id, d.title, ch.content, ch.token_count,
       ev.weight AS score
FROM ev
JOIN chunks ch ON ch.id = ev.chunk_id AND ch.org_id = app_current_org()
JOIN documents d ON d.id = ch.document_id
WHERE ch.acl_groups && CAST(:groups AS text[])
ORDER BY ev.weight DESC, ev.support DESC, ch.document_id, ch.ordinal
LIMIT :k
"""
# The worker: the oldest document waiting, or one whose extraction stopped mid-way (its lease ran
# out), taken so no other worker takes it.
_CLAIM: Final = """
UPDATE documents SET graph_status = 'running', graph_claimed_at = now()
WHERE id = (
  SELECT id FROM documents
  WHERE graph_status = 'pending'
     OR (graph_status = 'running' AND graph_claimed_at < now() - make_interval(secs => :lease))
  ORDER BY created_at, id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id AS document_id, org_id, domain_id
"""
_SCHEMA: Final = """
SELECT name, description, entity_types, relation_types FROM knowledge_domains WHERE id = :domain
"""
_CHUNKS: Final = """
SELECT id, content FROM chunks
WHERE document_id = :document AND org_id = :org
ORDER BY ordinal
"""
_PUT_ENTITIES: Final = """
INSERT INTO kg_entities (org_id, domain_id, type, name, norm)
SELECT :org, :domain, e.type, e.name, e.norm
FROM unnest(CAST(:types AS text[]), CAST(:names AS text[]), CAST(:norms AS text[]))
  AS e(type, name, norm)
ON CONFLICT (domain_id, type, norm) DO NOTHING
"""
_ENTITY_IDS: Final = """
SELECT e.id, e.type, e.norm FROM kg_entities e
JOIN unnest(CAST(:types AS text[]), CAST(:norms AS text[])) AS k(type, norm)
  ON e.type = k.type AND e.norm = k.norm
WHERE e.domain_id = :domain
"""
_PUT_RELATIONS: Final = """
INSERT INTO kg_relations (org_id, domain_id, src, dst, type)
SELECT :org, :domain, r.src, r.dst, r.type
FROM unnest(CAST(:srcs AS uuid[]), CAST(:dsts AS uuid[]), CAST(:types AS text[]))
  AS r(src, dst, type)
ON CONFLICT (src, dst, type) DO NOTHING
"""
_RELATION_IDS: Final = """
SELECT r.id, r.src, r.dst, r.type FROM kg_relations r
JOIN unnest(CAST(:srcs AS uuid[]), CAST(:dsts AS uuid[]), CAST(:types AS text[]))
  AS k(src, dst, type) ON r.src = k.src AND r.dst = k.dst AND r.type = k.type
"""
_PUT_EVIDENCE: Final = """
INSERT INTO kg_evidence (relation_id, chunk_id, document_id, org_id)
SELECT v.relation_id, v.chunk_id, :document, :org
FROM unnest(CAST(:relations AS uuid[]), CAST(:chunks AS uuid[])) AS v(relation_id, chunk_id)
ON CONFLICT (relation_id, chunk_id) DO NOTHING
"""
_READY: Final = """
UPDATE documents SET graph_status = 'ready', graph_tokens = :tokens, graph_error = NULL
WHERE id = :document
"""
_FAILED: Final = """
UPDATE documents SET graph_status = :status, graph_error = :error WHERE id = :document
"""


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(format(v, ".7g") for v in values) + "]"


def _domain(row: RowMapping) -> DomainRecord:
    return DomainRecord(
        row["id"],
        row["name"],
        row["description"],
        tuple(row["entity_types"]),
        tuple(row["relation_types"]),
        tuple(row["examples"]),
        float(row["intent_threshold"]),
        row["embedding_model"],
        row["created_at"],
    )


class KnowledgeRepository:
    async def create(
        self,
        conn: AsyncConnection,
        *,
        name: str,
        description: str,
        entity_types: Sequence[str],
        relation_types: Sequence[str],
        examples: Sequence[str],
        threshold: float,
        vector: Sequence[float],
        model: str,
        created_by: UUID | None,
    ) -> DomainRecord | None:
        """The new domain; None when the org has one of that name already."""
        params = {
            "name": name,
            "description": description,
            "entity_types": list(entity_types),
            "relation_types": list(relation_types),
            "examples": list(examples),
            "threshold": threshold,
            "vector": _vector(vector),
            "model": model,
            "created_by": created_by,
        }
        row = await fetch_one(conn, _CREATE, params)
        return _domain(row) if row is not None else None

    async def domains(self, conn: AsyncConnection) -> list[DomainRecord]:
        return [_domain(r) for r in await fetch_all(conn, _LIST, {})]

    async def by_name(self, conn: AsyncConnection, name: str) -> DomainRecord | None:
        row = await fetch_one(conn, _BY_NAME, {"name": name})
        return _domain(row) if row is not None else None

    async def delete(self, conn: AsyncConnection, domain_id: UUID) -> bool:
        """The domain and its graph; its documents stay, filed under no domain."""
        return await fetch_one(conn, _DELETE, {"id": domain_id}) is not None

    async def intent(
        self, conn: AsyncConnection, query: Sequence[float], *, model: str
    ) -> Intent | None:
        """The org's domain nearest the query, by its example questions."""
        row = await fetch_one(conn, _INTENT, {"query": _vector(query), "model": model})
        return Intent(_domain(row), float(row["score"])) if row is not None else None

    async def linked(
        self,
        conn: AsyncConnection,
        domain_id: UUID,
        terms: Sequence[str],
        *,
        groups: frozenset[str],
    ) -> list[Entity]:
        """The domain's entities the query names, among those the caller's documents state."""
        if not terms or not groups:
            return []
        params = {"domain": domain_id, "terms": list(terms), "groups": sorted(groups)}
        rows = await fetch_all(conn, _LINKED, params)
        return [Entity(r["id"], r["name"], r["type"]) for r in rows]

    async def candidates(
        self,
        conn: AsyncConnection,
        entities: Sequence[UUID],
        *,
        groups: frozenset[str],
        top: int,
    ) -> list[Candidate]:
        params = {"entities": list(entities), "groups": sorted(groups), "top": top}
        rows = await fetch_all(conn, _CANDIDATES, params)
        return [Candidate(Entity(r["id"], r["name"], r["type"]), int(r["matched"])) for r in rows]

    async def evidence(
        self,
        conn: AsyncConnection,
        centre: Sequence[tuple[UUID, float]],
        *,
        groups: frozenset[str],
        k: int,
    ) -> list[Hit]:
        """The chunks stating the relations of the centre's entities, scored by their weight."""
        if not centre or not groups:
            return []
        params = {
            "centre": [c[0] for c in centre],
            "weights": [c[1] for c in centre],
            "groups": sorted(groups),
            "k": k,
        }
        return [Hit(**dict(r)) for r in await fetch_all(conn, _EVIDENCE, params)]

    # --- the worker (role `worker`, across orgs) ---

    async def claim(self, conn: AsyncConnection, *, lease_s: int) -> Claim | None:
        row = await fetch_one(conn, _CLAIM, {"lease": lease_s})
        if row is None:
            return None
        return Claim(row["document_id"], row["org_id"], row["domain_id"])

    async def schema(self, conn: AsyncConnection, domain_id: UUID) -> DomainSchema | None:
        row = await fetch_one(conn, _SCHEMA, {"domain": domain_id})
        if row is None:
            return None
        return DomainSchema(
            row["name"],
            row["description"],
            tuple(row["entity_types"]),
            tuple(row["relation_types"]),
        )

    async def chunks(self, conn: AsyncConnection, claim: Claim) -> list[tuple[UUID, str]]:
        params = {"document": claim.document_id, "org": claim.org_id}
        return [(r["id"], r["content"]) for r in await fetch_all(conn, _CHUNKS, params)]

    async def store(
        self,
        conn: AsyncConnection,
        claim: Claim,
        domain_id: UUID,
        found: Sequence[tuple[UUID, Sequence[Relation]]],
        *,
        tokens: int,
    ) -> None:
        """A document's relations, chunk by chunk: each entity and relation once in the domain,
        and each chunk that states a relation as its evidence. Marks the document ready."""
        names: dict[tuple[str, str], str] = {}
        for _, relations in found:
            for r in relations:
                for m in (r.source, r.target):
                    names.setdefault((m.type, graph_norm(m.name)), m.name)
        if names:
            scope: dict[str, Any] = {"org": claim.org_id, "domain": domain_id}
            types = [k[0] for k in names]
            norms = [k[1] for k in names]
            entities = {**scope, "types": types, "names": list(names.values()), "norms": norms}
            await conn.execute(text(_PUT_ENTITIES), entities)
            rows = await fetch_all(
                conn, _ENTITY_IDS, {"domain": domain_id, "types": types, "norms": norms}
            )
            ids = {(r["type"], r["norm"]): r["id"] for r in rows}
            triples = [
                (
                    chunk,
                    ids[(r.source.type, graph_norm(r.source.name))],
                    ids[(r.target.type, graph_norm(r.target.name))],
                    r.type,
                )
                for chunk, relations in found
                for r in relations
            ]
            edges: dict[str, Any] = {
                "srcs": [t[1] for t in triples],
                "dsts": [t[2] for t in triples],
                "types": [t[3] for t in triples],
            }
            await conn.execute(text(_PUT_RELATIONS), {**scope, **edges})
            stored = await fetch_all(conn, _RELATION_IDS, edges)
            relation = {(r["src"], r["dst"], r["type"]): r["id"] for r in stored}
            evidence = {
                "document": claim.document_id,
                "org": claim.org_id,
                "relations": [relation[(t[1], t[2], t[3])] for t in triples],
                "chunks": [t[0] for t in triples],
            }
            await conn.execute(text(_PUT_EVIDENCE), evidence)
        await conn.execute(text(_READY), {"document": claim.document_id, "tokens": tokens})

    async def fail(
        self, conn: AsyncConnection, document_id: UUID, error: str, *, status: str
    ) -> None:
        """`failed` with the reason, or `none` when the document's domain is gone."""
        params = {"document": document_id, "error": error[:1000], "status": status}
        await conn.execute(text(_FAILED), params)
