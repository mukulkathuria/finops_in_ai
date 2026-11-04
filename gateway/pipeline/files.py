"""File uploads (US-14): validate from the bytes, store the original, record it.

The domain rules decide from the content; PDFs are probed in an isolated process; an optional
ClamAV scan fails closed. Nothing is stored before every check has passed, and the object key
never derives from the file name. A file is visible to the uploader's team and to the user who
uploaded it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID, uuid7

from gateway.adapters.files.clamav import ClamAV
from gateway.adapters.files.pdf import PdfProbe
from gateway.adapters.observability.metrics import GatewayMetrics
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.files import FileRecord, FileRepository, NewFile
from gateway.domain.errors import DomainError, InvalidInput, NotFound
from gateway.domain.files import (
    IMAGES,
    OFFICE,
    PURPOSES,
    SNIFF_BYTES,
    FileLimits,
    check_image,
    check_size,
    clean_filename,
    inspect_office,
    resolve_type,
    scan,
    sniff,
)
from gateway.ports.storage import ObjectStore

log = logging.getLogger(__name__)
_NOT_FOUND = "no such file"


@dataclass(frozen=True, slots=True)
class Owner:
    """Who acts on files: the org (row-level security), and the team and user that decide which
    files are visible."""

    org_id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None = None


def _size(file: BinaryIO) -> int:
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    return size


def _head(file: BinaryIO) -> bytes:
    file.seek(0)
    head = file.read(SNIFF_BYTES)
    file.seek(0)
    return head


class FileService:
    def __init__(
        self,
        *,
        db: Database,
        repo: FileRepository,
        store: ObjectStore,
        limits: FileLimits,
        pdf: PdfProbe,
        scanner: ClamAV | None,
        metrics: GatewayMetrics,
    ) -> None:
        self._db = db
        self._repo = repo
        self._store = store
        self._limits = limits
        self._pdf = pdf
        self._scanner = scanner
        self._metrics = metrics

    def _ctx(self, owner: Owner) -> TenantContext:
        return TenantContext(owner.org_id, owner.user_id)

    async def upload(
        self, owner: Owner, filename: str | None, file: BinaryIO, purpose: str
    ) -> FileRecord:
        try:
            record = await self._accept(owner, filename, file, purpose)
        except DomainError as exc:
            self._metrics.file_uploads.labels("rejected", exc.code).inc()
            raise
        self._metrics.file_uploads.labels("accepted", record.kind).inc()
        return record

    async def _accept(
        self, owner: Owner, filename: str | None, file: BinaryIO, purpose: str
    ) -> FileRecord:
        if purpose not in PURPOSES:
            raise InvalidInput(
                f"purpose must be one of: {', '.join(sorted(PURPOSES))}",
                code="invalid_purpose",
                param="purpose",
            )
        name = clean_filename(filename)
        size = await asyncio.to_thread(_size, file)
        check_size(size, self._limits)
        head = await asyncio.to_thread(_head, file)
        kind, mime = resolve_type(name, sniff(head))
        digest = await asyncio.to_thread(scan, file, text=kind == "text")
        pages: int | None = None
        if kind in IMAGES:
            check_image(kind, head, self._limits)
        elif kind in OFFICE:
            pages = await asyncio.to_thread(inspect_office, file, kind, self._limits)
        elif kind == "pdf":
            pages = await self._pdf.pages(file, self._limits)
        if self._scanner is not None:
            await self._scanner.check(file)
        file_id = uuid7()
        key = f"orgs/{owner.org_id}/files/{file_id}"
        await self._store.put(key, file, content_type=mime)
        new = NewFile(
            id=file_id,
            team_id=owner.team_id,
            user_id=owner.user_id,
            api_key_id=owner.api_key_id,
            filename=name,
            kind=kind,
            mime=mime,
            size=size,
            sha256=digest,
            pages=pages,
            purpose=purpose,
            storage_key=key,
        )
        try:
            async with self._db.transaction(self._ctx(owner)) as conn:
                return await self._repo.insert(conn, new)
        except Exception:
            await self._discard(key)  # the row was never written: its object must go too
            raise

    async def _discard(self, key: str) -> None:
        try:
            await self._store.delete(key)
        except OSError:
            log.warning("could not remove an unrecorded object", extra={"key": key})

    async def get(self, owner: Owner, file_id: UUID) -> FileRecord:
        async with self._db.transaction(self._ctx(owner)) as conn:
            record = await self._repo.get(
                conn, file_id, team_id=owner.team_id, user_id=owner.user_id
            )
        if record is None:
            raise NotFound(_NOT_FOUND, code="file_not_found")
        return record

    async def list(self, owner: Owner, *, limit: int, after: UUID | None) -> list[FileRecord]:
        async with self._db.transaction(self._ctx(owner)) as conn:
            return await self._repo.list(
                conn, team_id=owner.team_id, user_id=owner.user_id, after=after, limit=limit
            )

    async def content(self, owner: Owner, file_id: UUID) -> tuple[FileRecord, AsyncIterator[bytes]]:
        record = await self.get(owner, file_id)
        try:
            chunks = await self._store.stream(record.storage_key)
        except FileNotFoundError as exc:
            raise NotFound(
                "the file's content is no longer available", code="file_content_missing"
            ) from exc
        return record, chunks

    async def delete(self, owner: Owner, file_id: UUID) -> None:
        async with self._db.transaction(self._ctx(owner)) as conn:
            key = await self._repo.delete(
                conn, file_id, team_id=owner.team_id, user_id=owner.user_id
            )
        if key is None:
            raise NotFound(_NOT_FOUND, code="file_not_found")
        await self._discard(key)
