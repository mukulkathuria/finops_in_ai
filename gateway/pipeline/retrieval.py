"""Indexing and retrieval under the caller's permissions (spec section 4, flow F4; US-13).

Ingestion chunks a document (text, or a stored file converted to Markdown by the parse service),
embeds the chunks and stores them with the document's groups. Search embeds the query and asks
PostgreSQL for the nearest chunks the caller's groups may see, in one statement. For a chat
request, the hits become a lower-trust user-role message just before the question, so the
guardrails read them like anything a user sends.
"""

import logging
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
from gateway.adapters.postgres.relevance import RelevanceSettingsCache
from gateway.domain.errors import Forbidden, InvalidInput, NotFound
from gateway.domain.llm import CanonicalRequest, Message, TextPart
from gateway.domain.parsing import indexable_text, text_tokens
from gateway.domain.retrieval import (
    RAG_RELEVANCE,
    Hit,
    RelevanceSettings,
    chunk_text,
    pack,
    retrieved_context,
)
from gateway.domain.stages import Mode
from gateway.pipeline.files import Owner
from gateway.pipeline.parsing import ParseService
from gateway.pipeline.stages import StageRunner
from gateway.ports.embeddings import Embedder
from gateway.ports.rerank import Reranker

_MAX_QUERY_CHARS: Final = 4_000
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Plan:
    """How a retrieval runs for an org (US-21)."""

    mode: Mode  # of the `rag_relevance` stage; off without a reranker
    settings: RelevanceSettings
    candidates: int  # retrieved by vector


@dataclass(frozen=True, slots=True)
class Selection:
    passages: list[Hit]  # what is sent to the model
    report: dict[str, Any] | None  # for the ledger notes; None while the filter is off


@dataclass(frozen=True, slots=True)
class Author:
    owner: Owner
    groups: frozenset[str]  # the groups the document is shared with (already authorized)


@dataclass(frozen=True, slots=True)
class Ingested:
    record: DocumentRecord
    warnings: tuple[str, ...] = ()  # what was left out of the index, and why


def with_retrieved(req: CanonicalRequest, hits: Sequence[Hit]) -> CanonicalRequest:
    """The request with the retrieved passages before the last user message."""
    return with_reference(req, retrieved_context(hits)) if hits else req


def with_reference(req: CanonicalRequest, text: str) -> CanonicalRequest:
    """The request with reference data as a user-role message before the last user message
    (lower trust than the system prompt: the guardrails inspect it)."""
    context = Message(role="user", content=(TextPart(text),))
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
        stages: StageRunner | None = None,
        reranker: Reranker | None = None,
        relevance: RelevanceSettingsCache | None = None,
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
        self._stages = stages
        self._reranker = reranker
        self._relevance = relevance

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

    async def plan(self, org_id: UUID, k: int) -> Plan:
        """How this org's retrieval runs: the `rag_relevance` mode (off without a reranker), its
        settings and how many candidates to retrieve."""
        if self._reranker is None or self._stages is None or self._relevance is None:
            return Plan("off", RelevanceSettings(), min(k, self._max_k))
        mode = await self._stages.mode(org_id, RAG_RELEVANCE)
        if mode == "off":
            return Plan("off", RelevanceSettings(), min(k, self._max_k))
        settings = await self._relevance.get(org_id)
        return Plan(mode, settings, min(max(k, settings.candidates), self._max_k))

    async def select(self, question: str, hits: Sequence[Hit], *, k: int, plan: Plan) -> Selection:
        """US-21: the passages to send. On: the reranked passages over the org's threshold,
        packed within its token budget. In shadow: the k nearest, and the report says what would
        have been sent. A reranker failure keeps the k nearest (fail-open) and is reported."""
        nearest = list(hits[:k])
        if plan.mode == "off" or not hits or self._reranker is None:
            return Selection(nearest, None)
        base: dict[str, Any] = {
            "mode": plan.mode,
            "model": self._reranker.model,
            "candidates": len(hits),
        }
        try:
            scores = await self._reranker.rerank(
                question[:_MAX_QUERY_CHARS], [h.content for h in hits]
            )
        except Exception:  # fail-open: any reranker failure keeps the vector order
            log.warning("reranking failed; the vector order is kept", exc_info=True)
            return Selection(nearest, {**base, "rerank": "failed"})
        packed = pack(
            hits,
            scores,
            threshold=plan.settings.threshold,
            max_tokens=plan.settings.max_tokens,
            k=k,
        )
        report = {
            **base,
            "rerank": "ok",
            "threshold": plan.settings.threshold,
            "kept": len(packed.kept),
            "dropped": packed.dropped,
            "tokens_before": packed.tokens_before,
            "tokens_after": packed.tokens_after,
        }
        return Selection(list(packed.kept) if plan.mode == "on" else nearest, report)

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
