"""Indexing and retrieval under the caller's permissions (spec section 4, flow F4; US-13).

Ingestion chunks a document (text, or a stored file converted to Markdown by the parse service),
embeds the chunks and stores them with the document's groups. Search embeds the query and asks
PostgreSQL for the nearest chunks the caller's groups may see, in one statement. For a chat
request, the hits become a lower-trust user-role message just before the question, so the
guardrails read them like anything a user sends.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final
from uuid import UUID, uuid7

from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.documents import (
    DocumentRecord,
    DocumentRepository,
    NewChunk,
    NewDocument,
)
from gateway.domain.errors import Forbidden, InvalidInput, NotFound
from gateway.domain.llm import CanonicalRequest, Message, TextPart
from gateway.domain.parsing import indexable_text, text_tokens
from gateway.domain.retrieval import Hit, chunk_text, retrieved_context
from gateway.pipeline.files import Owner
from gateway.pipeline.parsing import ParseService
from gateway.ports.embeddings import Embedder

_MAX_QUERY_CHARS: Final = 4_000


@dataclass(frozen=True, slots=True)
class Author:
    owner: Owner
    groups: frozenset[str]  # the groups the document is shared with (already authorized)


@dataclass(frozen=True, slots=True)
class Ingested:
    record: DocumentRecord
    warnings: tuple[str, ...] = ()  # what was left out of the index, and why


def with_retrieved(req: CanonicalRequest, hits: Sequence[Hit]) -> CanonicalRequest:
    """The request with the retrieved passages as a user-role message before the last user
    message (lower trust than the system prompt: the guardrails inspect it)."""
    if not hits:
        return req
    context = Message(role="user", content=(TextPart(retrieved_context(hits)),))
    messages = list(req.messages)
    last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=len(messages))
    messages.insert(last_user, context)
    return replace(req, messages=tuple(messages))


def retrieval_report(hits: Sequence[Hit]) -> dict[str, Any]:
    return {
        "chunks": [str(h.chunk_id) for h in hits],
        "documents": sorted({str(h.document_id) for h in hits}),
        "tokens": sum(h.token_count for h in hits),
    }


class RetrievalService:
    def __init__(
        self,
        *,
        db: Database,
        repo: DocumentRepository,
        embedder: Embedder,
        parsing: ParseService,
        metrics: GatewayMetrics,
        max_k: int = 50,
        max_chunks_per_org: int = 1_000_000,
    ) -> None:
        self._db = db
        self._repo = repo
        self._embedder = embedder
        self._parsing = parsing
        self._metrics = metrics
        self._max_k = max_k
        self._max_chunks = max_chunks_per_org

    def _ctx(self, owner: Owner) -> TenantContext:
        return TenantContext(owner.org_id, owner.user_id)

    async def ingest(
        self, author: Author, title: str, text: str | None, file_id: UUID | None
    ) -> Ingested:
        if (text is None) == (file_id is None):
            raise InvalidInput("send either text or file_id", param="text")
        warnings: tuple[str, ...] = ()
        if file_id is not None:
            artifact = (await self._parsing.derive(author.owner, file_id)).artifact
            text, kept = indexable_text(artifact.markdown, artifact.pages)
            if kept:
                pages = ", ".join(map(str, kept))
                warnings = (f"pages read with low confidence are not indexed: {pages}",)
        chunks = chunk_text(text or "")
        if not chunks:
            reason = "; pages read with low confidence are not indexed" if warnings else ""
            raise InvalidInput(f"the document has no text to index{reason}", code="empty_document")
        owner = author.owner
        async with self._db.transaction(self._ctx(owner)) as conn:
            held = await self._repo.indexed_chunks(conn)
        # shortcut: concurrent ingests can pass the quota together by their own sizes; exact
        # accounting would need a per-org lock held across the (slow) embedding call.
        if held + len(chunks) > self._max_chunks:
            raise Forbidden(
                f"the organisation's index holds {held} chunks and this document adds"
                f" {len(chunks)}, past the limit of {self._max_chunks}; delete documents first",
                code="index_quota_exceeded",
            )
        vectors = await self._embedder.embed(chunks)
        counted = [text_tokens(c) for c in chunks]
        document = NewDocument(
            id=uuid7(),
            title=title,
            file_id=file_id,
            acl_groups=sorted(author.groups),
            created_by=owner.user_id,
            api_key_id=owner.api_key_id,
            embedding_model=self._embedder.model,
            chunk_count=len(chunks),
            token_count=sum(counted),
        )
        rows = [
            NewChunk(i, chunk, tokens, vector)
            for i, (chunk, tokens, vector) in enumerate(zip(chunks, counted, vectors, strict=True))
        ]
        async with self._db.transaction(self._ctx(owner)) as conn:
            record = await self._repo.insert(conn, document, rows)
        self._metrics.retrievals.labels("ingest").inc()
        return Ingested(record, warnings)

    async def search(
        self, owner: Owner, groups: frozenset[str], query: str, k: int, *, source: str = "api"
    ) -> list[Hit]:
        query = query.strip()[:_MAX_QUERY_CHARS]
        if not query:
            return []
        (vector,) = await self._embedder.embed([query])
        async with self._db.transaction(self._ctx(owner)) as conn:
            hits = await self._repo.search(
                conn, vector, groups=groups, model=self._embedder.model, k=min(k, self._max_k)
            )
        self._metrics.retrievals.labels(source).inc()
        return hits

    async def documents(
        self, owner: Owner, groups: frozenset[str], *, limit: int
    ) -> list[DocumentRecord]:
        async with self._db.transaction(self._ctx(owner)) as conn:
            return await self._repo.visible(conn, groups=groups, limit=limit)

    async def delete(self, owner: Owner, document_id: UUID, *, admin: bool) -> None:
        async with self._db.transaction(self._ctx(owner)) as conn:
            deleted = await self._repo.delete(
                conn,
                document_id,
                admin=admin,
                user_id=owner.user_id,
                api_key_id=owner.api_key_id,
            )
        if not deleted:
            raise NotFound("no such document", code="document_not_found")
