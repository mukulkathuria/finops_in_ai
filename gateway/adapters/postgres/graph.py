"""The PostgreSQL graph store (ADR 0004). Fixed-depth patterns are explicit indexed joins (what
the SQL/PGQ rewriter produced; PostgreSQL 19 ships without it) and variable depth is a recursive
CTE whose UNION over (vertex, depth) keeps cycles finite. Every read checks the project's ACL in
the same statement, under row-level security. The writer replaces a project's graph at once, so
a reader sees the old graph or the new one, never a mix."""

from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

import orjson
from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import Database, TenantContext, fetch_all, fetch_one
from gateway.domain.errors import NotFound
from gateway.domain.graph import (
    MAX_DEPTH,
    MAX_FIXED_DEPTH,
    Direction,
    Edge,
    Reached,
    Vertex,
    check_graph,
    check_read,
)

_OUT_1: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props, e1.confidence
  FROM graph_edge e1
  JOIN graph_vertex v ON v.id = e1.dst
  WHERE e1.src = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, e1.confidence DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_OUT_2: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props,
         least(e1.confidence, e2.confidence) AS confidence
  FROM graph_edge e1
  JOIN graph_edge e2 ON e2.src = e1.dst AND e2.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_vertex v ON v.id = e2.dst
  WHERE e1.src = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, least(e1.confidence, e2.confidence) DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_OUT_3: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props,
         least(e1.confidence, e2.confidence, e3.confidence) AS confidence
  FROM graph_edge e1
  JOIN graph_edge e2 ON e2.src = e1.dst AND e2.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_edge e3 ON e3.src = e2.dst AND e3.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_vertex v ON v.id = e3.dst
  WHERE e1.src = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, least(e1.confidence, e2.confidence, e3.confidence) DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_IN_1: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props, e1.confidence
  FROM graph_edge e1
  JOIN graph_vertex v ON v.id = e1.src
  WHERE e1.dst = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, e1.confidence DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_IN_2: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props,
         least(e1.confidence, e2.confidence) AS confidence
  FROM graph_edge e1
  JOIN graph_edge e2 ON e2.dst = e1.src AND e2.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_vertex v ON v.id = e2.src
  WHERE e1.dst = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, least(e1.confidence, e2.confidence) DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_IN_3: Final = """
SELECT id, kind, name, path, props, confidence FROM (
  SELECT DISTINCT ON (v.id) v.id, v.kind, v.name, v.path, v.props,
         least(e1.confidence, e2.confidence, e3.confidence) AS confidence
  FROM graph_edge e1
  JOIN graph_edge e2 ON e2.dst = e1.src AND e2.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_edge e3 ON e3.dst = e2.src AND e3.kind = ANY(CAST(:kinds AS text[]))
  JOIN graph_vertex v ON v.id = e3.src
  WHERE e1.dst = ANY(CAST(:ids AS uuid[])) AND e1.kind = ANY(CAST(:kinds AS text[]))
    AND e1.project_id = :project
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  ORDER BY v.id, least(e1.confidence, e2.confidence, e3.confidence) DESC
) best
ORDER BY confidence DESC, name
LIMIT :limit
"""
_REACH_OUT: Final = """
WITH RECURSIVE walk (id, depth) AS (
  SELECT e.dst, 1
  FROM graph_edge e
  WHERE e.src = ANY(CAST(:ids AS uuid[])) AND e.kind = ANY(CAST(:kinds AS text[]))
    AND e.project_id = :project AND e.confidence >= :floor
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  UNION
  SELECT e.dst, w.depth + 1
  FROM walk w
  JOIN graph_edge e ON e.src = w.id AND e.kind = ANY(CAST(:kinds AS text[]))
   AND e.confidence >= :floor
  WHERE w.depth < :max_depth
)
SELECT v.id, v.kind, v.name, v.path, v.props, min(w.depth) AS depth
FROM walk w
JOIN graph_vertex v ON v.id = w.id
GROUP BY v.id
ORDER BY depth, v.name
LIMIT :limit
"""
_REACH_IN: Final = """
WITH RECURSIVE walk (id, depth) AS (
  SELECT e.src, 1
  FROM graph_edge e
  WHERE e.dst = ANY(CAST(:ids AS uuid[])) AND e.kind = ANY(CAST(:kinds AS text[]))
    AND e.project_id = :project AND e.confidence >= :floor
    AND EXISTS (SELECT 1 FROM projects p
                WHERE p.id = :project AND p.acl_groups && CAST(:groups AS text[]))
  UNION
  SELECT e.src, w.depth + 1
  FROM walk w
  JOIN graph_edge e ON e.dst = w.id AND e.kind = ANY(CAST(:kinds AS text[]))
   AND e.confidence >= :floor
  WHERE w.depth < :max_depth
)
SELECT v.id, v.kind, v.name, v.path, v.props, min(w.depth) AS depth
FROM walk w
JOIN graph_vertex v ON v.id = w.id
GROUP BY v.id
ORDER BY depth, v.name
LIMIT :limit
"""
_NEIGHBORS: Final[Mapping[tuple[Direction, int], str]] = {
    ("out", 1): _OUT_1,
    ("out", 2): _OUT_2,
    ("out", 3): _OUT_3,
    ("in", 1): _IN_1,
    ("in", 2): _IN_2,
    ("in", 3): _IN_3,
}
_REACH: Final[Mapping[Direction, str]] = {"out": _REACH_OUT, "in": _REACH_IN}
_PGQ: Final = "SELECT to_regclass('pg_catalog.pg_propgraph_element') IS NOT NULL AS present"

_PROJECT: Final = """
INSERT INTO projects (org_id, name, acl_groups, created_by)
VALUES (app_current_org(), :name, CAST(:groups AS text[]), :created_by)
RETURNING id
"""
_LOCK_PROJECT: Final = "SELECT id FROM projects WHERE id = :project FOR UPDATE"
_CLEAR_EDGES: Final = "DELETE FROM graph_edge WHERE project_id = :project"
_CLEAR_PACKS: Final = "DELETE FROM context_packs WHERE project_id = :project"
_CLEAR_VERTICES: Final = "DELETE FROM graph_vertex WHERE project_id = :project"
_VERTEX: Final = """
INSERT INTO graph_vertex (id, org_id, project_id, kind, name, path, props)
VALUES (:id, app_current_org(), :project, :kind, :name, :path, CAST(:props AS jsonb))
"""
_EDGE: Final = """
INSERT INTO graph_edge (org_id, project_id, src, dst, kind, confidence)
VALUES (app_current_org(), :project, :src, :dst, :kind, :confidence)
"""
_INDEXED: Final = "UPDATE projects SET indexed_at = now() WHERE id = :project RETURNING id"


def _vertex(row: RowMapping) -> Vertex:
    props = row["props"]
    return Vertex(
        row["id"], row["kind"], row["name"], row["path"], props if isinstance(props, dict) else {}
    )


class PostgresGraphStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._pgq: bool | None = None

    async def neighbors(
        self,
        org_id: UUID,
        *,
        project_id: UUID,
        groups: frozenset[str],
        ids: Sequence[UUID],
        kinds: Sequence[str],
        direction: Direction = "out",
        depth: int = 1,
        limit: int = 200,
    ) -> list[Reached]:
        check_read(kinds, depth, limit, max_depth=MAX_FIXED_DEPTH)
        sql = _NEIGHBORS.get((direction, depth))
        if sql is None:
            raise ValueError("direction is out or in")
        if not ids or not groups:
            return []
        params = {
            "ids": list(ids),
            "kinds": sorted(set(kinds)),
            "project": project_id,
            "groups": sorted(groups),
            "limit": limit,
        }
        async with self._db.transaction(TenantContext(org_id)) as conn:
            rows = await fetch_all(conn, sql, params)
        return [Reached(_vertex(r), depth, float(r["confidence"])) for r in rows]

    async def reachable(
        self,
        org_id: UUID,
        *,
        project_id: UUID,
        groups: frozenset[str],
        ids: Sequence[UUID],
        kinds: Sequence[str],
        direction: Direction = "out",
        max_depth: int = 6,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[Reached]:
        check_read(kinds, max_depth, limit, max_depth=MAX_DEPTH)
        sql = _REACH.get(direction)
        if sql is None:
            raise ValueError("direction is out or in")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence is between 0 and 1")
        if not ids or not groups:
            return []
        params = {
            "ids": list(ids),
            "kinds": sorted(set(kinds)),
            "project": project_id,
            "groups": sorted(groups),
            "limit": limit,
            "max_depth": max_depth,
            "floor": min_confidence,
        }
        async with self._db.transaction(TenantContext(org_id)) as conn:
            rows = await fetch_all(conn, sql, params)
        return [Reached(_vertex(r), int(r["depth"])) for r in rows]

    async def sql_pgq(self) -> bool | None:
        """Whether the server has SQL/PGQ (ADR 0004 probe), cached once known; None when the
        probe cannot run (it never decides readiness)."""
        if self._pgq is None:
            try:
                async with self._db.transaction() as conn:
                    row = await fetch_one(conn, _PGQ)
            except SQLAlchemyError, OSError, TimeoutError:
                return None
            self._pgq = bool(row is not None and row["present"])
        return self._pgq


class GraphRepository:
    """The code graph's writes: a project, and its whole graph replaced in one transaction."""

    async def create_project(
        self, conn: AsyncConnection, *, name: str, groups: Sequence[str], created_by: UUID | None
    ) -> UUID:
        row = await fetch_one(
            conn, _PROJECT, {"name": name, "groups": sorted(groups), "created_by": created_by}
        )
        if row is None:
            raise RuntimeError("project insert returned no row")
        project_id: UUID = row["id"]
        return project_id

    async def replace_graph(
        self,
        conn: AsyncConnection,
        *,
        project_id: UUID,
        vertices: Sequence[Vertex],
        edges: Sequence[Edge],
    ) -> None:
        check_graph(vertices, edges)
        if await fetch_one(conn, _LOCK_PROJECT, {"project": project_id}) is None:
            raise NotFound("project not found", code="project_not_found")
        for clear in (_CLEAR_EDGES, _CLEAR_PACKS, _CLEAR_VERTICES):
            await conn.execute(text(clear), {"project": project_id})
        if vertices:
            await conn.execute(
                text(_VERTEX),
                [
                    {
                        "id": v.id,
                        "project": project_id,
                        "kind": v.kind,
                        "name": v.name,
                        "path": v.path,
                        "props": orjson.dumps(dict(v.props)).decode(),
                    }
                    for v in vertices
                ],
            )
        if edges:
            await conn.execute(
                text(_EDGE),
                [
                    {
                        "project": project_id,
                        "src": e.src,
                        "dst": e.dst,
                        "kind": e.kind,
                        "confidence": e.confidence,
                    }
                    for e in edges
                ],
            )
        await fetch_one(conn, _INDEXED, {"project": project_id})
