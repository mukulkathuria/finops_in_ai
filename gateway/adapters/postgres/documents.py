"""Documents and chunks for retrieval (US-13). Rows are org-scoped by RLS, and the ACL filter
sits in the same statement as the vector search, never after it (spec section 3A). Iterative
index scans keep the search complete when the filter is selective; the materialised hits are
re-ordered by distance, as relaxed ordering requires."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid7

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.adapters.postgres.database import fetch_all, fetch_one
from gateway.domain.retrieval import Hit


@dataclass(frozen=True, slots=True)
class NewDocument:
    id: UUID
    title: str
    file_id: UUID | None
    acl_groups: list[str]
    created_by: UUID | None
    api_key_id: UUID | None
    embedding_model: str
    chunk_count: int
    token_count: int


@dataclass(frozen=True, slots=True)
class NewChunk:
    ordinal: int
    content: str
    token_count: int
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: UUID
    title: str
    file_id: UUID | None
    acl_groups: list[str]
    created_by: UUID | None
    api_key_id: UUID | None
    embedding_model: str
    chunk_count: int
    token_count: int
    created_at: datetime


_INSERT_DOCUMENT: Final = """
INSERT INTO documents (id, org_id, title, file_id, acl_groups, created_by, api_key_id,
                       embedding_model, chunk_count, token_count)
VALUES (:id, app_current_org(), :title, :file_id, CAST(:acl_groups AS text[]), :created_by,
        :api_key_id, :embedding_model, :chunk_count, :token_count)
RETURNING id, title, file_id, acl_groups, created_by, api_key_id, embedding_model, chunk_count,
          token_count, created_at
"""
_INSERT_CHUNK: Final = """
INSERT INTO chunks (id, org_id, document_id, ordinal, content, token_count, acl_groups,
                    embedding_model, embedding)
VALUES (:id, app_current_org(), :document_id, :ordinal, :content, :token_count,
        CAST(:acl_groups AS text[]), :embedding_model, CAST(:embedding AS vector))
"""
_TUNE: Final = (
    "SET LOCAL hnsw.iterative_scan = relaxed_order",
    "SET LOCAL hnsw.ef_search = 100",
)
_SEARCH: Final = """
WITH hits AS MATERIALIZED (
  SELECT id, document_id, content, token_count,
         embedding <=> CAST(:query AS vector) AS distance
  FROM chunks
  WHERE embedding_model = :model AND acl_groups && CAST(:groups AS text[])
  ORDER BY embedding <=> CAST(:query AS vector)
  LIMIT :k
)
SELECT h.id AS chunk_id, h.document_id, d.title, h.content, h.token_count,
       1 - h.distance AS score
FROM hits h JOIN documents d ON d.id = h.document_id
ORDER BY h.distance
"""
_LIST: Final = """
SELECT id, title, file_id, acl_groups, created_by, api_key_id, embedding_model, chunk_count,
       token_count, created_at
FROM documents
WHERE acl_groups && CAST(:groups AS text[])
ORDER BY id DESC
LIMIT :limit
"""
_DELETE: Final = """
DELETE FROM documents
WHERE id = :id
  AND (CAST(:admin AS boolean)
       OR (created_by IS NOT NULL AND created_by = :user_id)
       OR (created_by IS NULL AND api_key_id = :api_key_id))
RETURNING id
"""


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(format(v, ".7g") for v in values) + "]"


class DocumentRepository:
    async def insert(
        self, conn: AsyncConnection, document: NewDocument, chunks: Sequence[NewChunk]
    ) -> DocumentRecord:
        row = await fetch_one(
            conn,
            _INSERT_DOCUMENT,
            {
                "id": document.id,
                "title": document.title,
                "file_id": document.file_id,
                "acl_groups": document.acl_groups,
                "created_by": document.created_by,
                "api_key_id": document.api_key_id,
                "embedding_model": document.embedding_model,
                "chunk_count": document.chunk_count,
                "token_count": document.token_count,
            },
        )
        if row is None:
            raise RuntimeError("document insert returned no row")
        await conn.execute(
            text(_INSERT_CHUNK),
            [
                {
                    "id": uuid7(),
                    "document_id": document.id,
                    "ordinal": c.ordinal,
                    "content": c.content,
                    "token_count": c.token_count,
                    "acl_groups": document.acl_groups,
                    "embedding_model": document.embedding_model,
                    "embedding": _vector(c.embedding),
                }
                for c in chunks
            ],
        )
        return DocumentRecord(**dict(row))

    async def search(
        self,
        conn: AsyncConnection,
        query: Sequence[float],
        *,
        groups: frozenset[str],
        model: str,
        k: int,
    ) -> list[Hit]:
        for statement in _TUNE:
            await conn.execute(text(statement))
        rows = await fetch_all(
            conn,
            _SEARCH,
            {"query": _vector(query), "groups": sorted(groups), "model": model, "k": k},
        )
        return [Hit(**dict(row)) for row in rows]

    async def visible(
        self, conn: AsyncConnection, *, groups: frozenset[str], limit: int
    ) -> list[DocumentRecord]:
        rows = await fetch_all(conn, _LIST, {"groups": sorted(groups), "limit": limit})
        return [DocumentRecord(**dict(row)) for row in rows]

    async def delete(
        self,
        conn: AsyncConnection,
        document_id: UUID,
        *,
        admin: bool,
        user_id: UUID | None,
        api_key_id: UUID | None,
    ) -> bool:
        row = await fetch_one(
            conn,
            _DELETE,
            {"id": document_id, "admin": admin, "user_id": user_id, "api_key_id": api_key_id},
        )
        return row is not None
