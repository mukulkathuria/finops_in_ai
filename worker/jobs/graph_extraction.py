"""Reading knowledge-domain documents into their graph (ADR 0011), one document at a time.

- A document filed under a domain waits as `pending`. The worker claims it with a lease (a
  crashed worker's claim runs out and is taken again).
- The extraction model reads its chunks, a few at once.
- Its entities, relations and evidence are stored in one transaction, and it becomes `ready`.
- If the model cannot answer, the document is `failed`, with the reason; nothing of it is
  stored. If its domain was deleted meanwhile, it is `none`."""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from gateway.adapters.postgres.database import Database
from gateway.adapters.postgres.knowledge import KnowledgeRepository
from gateway.domain.knowledge import Extraction, ExtractionFailed
from gateway.ports.graph_extraction import GraphExtractor

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    document_id: UUID
    status: str  # ready | failed | none
    relations: int
    tokens: int  # sent to and returned by the extraction model


class GraphExtraction:
    def __init__(
        self,
        db: Database,
        extractor: GraphExtractor,
        *,
        concurrency: int = 4,
        lease_s: int = 1_800,
    ) -> None:
        self._db = db
        self._extractor = extractor
        self._concurrency = concurrency
        self._lease = lease_s
        self._repo = KnowledgeRepository()

    async def run_once(self) -> ExtractionReport | None:
        """Extract the next waiting document; None when none waits."""
        async with self._db.transaction() as conn:
            claim = await self._repo.claim(conn, lease_s=self._lease)
        if claim is None:
            return None
        async with self._db.transaction() as conn:
            schema = await self._repo.schema(conn, claim.domain_id) if claim.domain_id else None
            chunks = await self._repo.chunks(conn, claim) if schema is not None else []
        if schema is None or claim.domain_id is None:
            async with self._db.transaction() as conn:
                await self._repo.fail(
                    conn, claim.document_id, "its knowledge domain was deleted", status="none"
                )
            return ExtractionReport(claim.document_id, "none", 0, 0)
        gate = asyncio.Semaphore(self._concurrency)

        async def read(content: str) -> Extraction:
            async with gate:
                return await self._extractor.extract(schema, content)

        results = await asyncio.gather(
            *(read(content) for _, content in chunks), return_exceptions=True
        )
        failed = next((r for r in results if isinstance(r, BaseException)), None)
        if failed is not None:
            if not isinstance(failed, ExtractionFailed):
                raise failed
            async with self._db.transaction() as conn:
                await self._repo.fail(conn, claim.document_id, str(failed), status="failed")
            log.warning(
                "graph extraction failed",
                extra={"document_id": str(claim.document_id), "reason": str(failed)},
            )
            return ExtractionReport(claim.document_id, "failed", 0, 0)
        read_chunks = [
            (chunk_id, r)
            for (chunk_id, _), r in zip(chunks, results, strict=True)
            if isinstance(r, Extraction)
        ]
        tokens = sum(r.tokens for _, r in read_chunks)
        found = [(chunk_id, r.relations) for chunk_id, r in read_chunks if r.relations]
        async with self._db.transaction() as conn:
            await self._repo.store(conn, claim, claim.domain_id, found, tokens=tokens)
        return ExtractionReport(
            claim.document_id, "ready", sum(len(relations) for _, relations in found), tokens
        )
