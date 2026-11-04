"""Attachments converted to Markdown (spec section 4, flow F3; US-15, US-16).

A file is parsed once per content hash, backend, backend version and options, and the result is
kept in the parse cache (per org): the two OCR backends' results are never mixed, and switching
OCR_BACKEND re-parses on demand. PDFs and images go to the OCR backend, which reads a page's text
layer when it has one; Office files and HTML go to MarkItDown; plain text stays as it is. A page
read below OCR_MIN_CONFIDENCE keeps its original image, attached for a vision model instead of
unreliable text. Every derivation reports what the original would cost in tokens and what the
derived form costs.
"""

import asyncio
import base64
import html
import io
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid7

from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.adapters.parsers.registry import Parsers
from gateway.adapters.postgres.artifacts import (
    Artifact,
    ArtifactKey,
    ArtifactRepository,
    NewArtifact,
)
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.files import FileRecord
from gateway.domain.errors import InvalidInput, NotFound, Unavailable
from gateway.domain.files import IMAGES, SNIFF_BYTES, image_size
from gateway.domain.llm import ContentPart, ImagePart, TextPart
from gateway.domain.parsing import (
    PAGE_IMAGE_TOKENS,
    ParsedDocument,
    ParseOptions,
    StoredDocument,
    image_tokens,
    text_tokens,
)
from gateway.domain.retrieval import neutralize_frames
from gateway.pipeline.files import FileService, Owner
from gateway.ports.parsers import DocumentParser
from gateway.ports.storage import ObjectStore

log = logging.getLogger(__name__)
MAX_FILES_PER_REQUEST: Final = 20


def file_id_of(reference: str) -> UUID:
    """The id inside an OpenAI-style `file-<hex>` reference; an unusable one is simply unknown."""
    try:
        return UUID(reference.removeprefix("file-"))
    except ValueError:
        raise NotFound("no such file", code="file_not_found") from None


@dataclass(frozen=True, slots=True)
class Derived:
    file: FileRecord
    artifact: Artifact
    cached: bool

    def report(self) -> dict[str, Any]:
        a = self.artifact
        return {
            "file_id": f"file-{self.file.id.hex}",
            "backend": a.backend,
            "backend_version": a.backend_version,
            "tokens_before": a.tokens_before,
            "tokens_after": a.tokens_after,
            "cached": self.cached,
            "kept_pages": [p["page"] for p in a.pages if p.get("kept_original")],
        }


@dataclass(frozen=True, slots=True)
class Attached:
    """A chat request's file parts, resolved: the content parts for each file reference, and one
    report per file for the response headers and the ledger row."""

    parts: Mapping[str, tuple[ContentPart, ...]]
    reports: tuple[dict[str, Any], ...] = ()

    @property
    def tokens_before(self) -> int:
        return sum(int(r["tokens_before"]) for r in self.reports)

    @property
    def tokens_after(self) -> int:
        return sum(int(r["tokens_after"]) for r in self.reports)


def _tokens(record: FileRecord, data: bytes, parsed: ParsedDocument) -> tuple[int, int]:
    """(before, after): the original as a model would read it, and the derived form."""
    kept = sum(1 for p in parsed.pages if p.kept_original)
    if record.kind == "pdf":
        before, per_kept = PAGE_IMAGE_TOKENS * len(parsed.pages), PAGE_IMAGE_TOKENS
    elif record.kind in IMAGES:
        size = image_size(record.kind, data[:SNIFF_BYTES]) or (0, 0)
        before = per_kept = image_tokens(*size)
    elif record.mime == "text/html":
        before, per_kept = text_tokens(data.decode("utf-8", "replace")), 0
    else:  # plain text and Office files: a model reads their text either way
        before, per_kept = text_tokens(parsed.markdown), 0
    return before, text_tokens(parsed.markdown) + per_kept * kept


class ParseService:
    def __init__(
        self,
        *,
        db: Database,
        files: FileService,
        artifacts: ArtifactRepository,
        store: ObjectStore,
        parsers: Parsers,
        min_confidence: float,
        metrics: GatewayMetrics,
    ) -> None:
        self._db = db
        self._files = files
        self._artifacts = artifacts
        self._store = store
        self._parsers = parsers
        self._min_confidence = min_confidence
        self._metrics = metrics
        self._inflight: dict[tuple[UUID, ArtifactKey], asyncio.Task[Artifact]] = {}

    def _ctx(self, owner: Owner) -> TenantContext:
        return TenantContext(owner.org_id, owner.user_id)

    async def derive(self, owner: Owner, file_id: UUID) -> Derived:
        """The file's derived artifact: from the cache, or parsed now (once per document at a
        time in this process; parallel callers share the parse)."""
        record = await self._files.get(owner, file_id)
        parser = self._parsers.for_document(record.mime)
        options = ParseOptions(min_confidence=self._min_confidence)
        try:
            version = await parser.version()
        except Unavailable:
            stale = await self._latest(owner, record, parser, options)
            if stale is None:
                raise
            log.warning(
                "parser unavailable; serving the last artifact", extra={"file": str(file_id)}
            )
            return Derived(record, stale, cached=True)
        key = ArtifactKey(record.sha256, parser.name, version, options.digest())
        async with self._db.transaction(self._ctx(owner)) as conn:
            cached = await self._artifacts.get(conn, key)
        if cached is not None:
            self._metrics.document_parses.labels(parser.name, "cached").inc()
            return Derived(record, cached, cached=True)
        flight = (owner.org_id, key)
        task = self._inflight.get(flight)
        if task is None:
            task = asyncio.create_task(self._build(owner, record, parser, options, key))
            self._inflight[flight] = task
            task.add_done_callback(lambda done: self._landed(flight, done))
        # A caller that goes away does not stop the parse: its result lands in the cache.
        return Derived(record, await asyncio.shield(task), cached=False)

    async def _latest(
        self, owner: Owner, record: FileRecord, parser: DocumentParser, options: ParseOptions
    ) -> Artifact | None:
        key = ArtifactKey(record.sha256, parser.name, "", options.digest())
        async with self._db.transaction(self._ctx(owner)) as conn:
            return await self._artifacts.latest(conn, key)

    def _landed(self, flight: tuple[UUID, ArtifactKey], task: asyncio.Task[Artifact]) -> None:
        if self._inflight.get(flight) is task:
            del self._inflight[flight]
        if not task.cancelled():
            task.exception()  # retrieved here, so a failure nobody awaited is not reported lost

    async def drain(self, timeout_s: float = 30.0) -> None:
        """Wait for parses in flight (graceful shutdown), but not past `timeout_s`."""
        if self._inflight:
            await asyncio.wait(set(self._inflight.values()), timeout=timeout_s)

    async def _build(
        self,
        owner: Owner,
        record: FileRecord,
        parser: DocumentParser,
        options: ParseOptions,
        key: ArtifactKey,
    ) -> Artifact:
        try:
            data = await self._read(record.storage_key)
            document = StoredDocument(record.filename, record.kind, record.mime, data)
            parsed = await parser.parse(document, options)
        except Exception:
            self._metrics.document_parses.labels(parser.name, "failed").inc()
            raise
        artifact_id = uuid7()
        images: list[str] = []
        try:
            pages: list[dict[str, Any]] = []
            for page in parsed.pages:
                image_key = None
                if page.kept_original and page.image is not None:
                    image_key = f"orgs/{owner.org_id}/artifacts/{artifact_id}/page-{page.page}"
                    await self._store.put(
                        image_key, io.BytesIO(page.image), content_type="image/png"
                    )
                    images.append(image_key)
                pages.append(
                    {
                        "page": page.page,
                        "method": page.method,
                        "confidence": round(page.confidence, 4),
                        "kept_original": page.kept_original,
                        "image_key": image_key,
                    }
                )
            before, after = _tokens(record, data, parsed)
            new = NewArtifact(
                artifact_id,
                key,
                parsed.markdown,
                pages,
                list(parsed.warnings),
                before,
                after,
                parsed.gpu_seconds,
            )
            async with self._db.transaction(self._ctx(owner)) as conn:
                artifact, created = await self._artifacts.insert(conn, new)
        except Exception:
            await self._discard(images)
            raise
        if not created:  # a parallel parse (another replica) stored it first: its artifact wins
            await self._discard(images)
        self._metrics.document_parses.labels(parser.name, "parsed").inc()
        return artifact

    async def _discard(self, keys: Sequence[str]) -> None:
        for key in keys:
            try:
                await self._store.delete(key)
            except OSError:
                log.warning("could not remove an unrecorded page image", extra={"key": key})

    async def _read(self, key: str) -> bytes:
        try:
            chunks = await self._store.stream(key)
        except FileNotFoundError as exc:
            raise NotFound(
                "the file's content is no longer available", code="file_content_missing"
            ) from exc
        return b"".join([chunk async for chunk in chunks])

    async def attach(self, owner: Owner, references: Sequence[str]) -> Attached:
        """Resolve a chat request's file references into content parts: the Markdown in a
        document frame, plus the original image of every page that was kept."""
        if not references:
            return Attached({})
        if len(references) > MAX_FILES_PER_REQUEST:
            raise InvalidInput(
                f"at most {MAX_FILES_PER_REQUEST} files may be attached to one request",
                code="too_many_files",
                param="messages",
            )
        results = await asyncio.gather(
            *(self.derive(owner, file_id_of(r)) for r in references), return_exceptions=True
        )
        derived: list[Derived] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            derived.append(result)
        parts = {r: await self._parts(d) for r, d in zip(references, derived, strict=True)}
        return Attached(parts, tuple(d.report() for d in derived))

    async def _parts(self, derived: Derived) -> tuple[ContentPart, ...]:
        record, artifact = derived.file, derived.artifact
        name = html.escape(record.filename, quote=True)
        body = neutralize_frames(artifact.markdown)  # the document cannot close its frame
        parts: list[ContentPart] = [TextPart(f'<document name="{name}">\n{body}\n</document>')]
        for page in artifact.pages:
            if not page.get("kept_original"):
                continue
            if page.get("image_key"):
                data, mime = await self._read(str(page["image_key"])), "image/png"
            elif record.kind in IMAGES:
                data, mime = await self._read(record.storage_key), record.mime
            else:
                continue
            parts.append(ImagePart(f"data:{mime};base64,{base64.b64encode(data).decode()}"))
        return tuple(parts)
