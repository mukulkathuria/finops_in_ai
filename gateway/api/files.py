"""OpenAI-compatible file endpoints (US-14): upload, list, read, download and delete.

A file is visible to the uploader's team and to the user who uploaded it. An upload is validated
from its bytes before anything is stored; a refusal names its reason in `error.code`.
Downloads are attachments with `nosniff` and a sandboxing CSP, so an uploaded HTML file can
never run in the dashboard's origin.
"""

from typing import Annotated, Literal, Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from gateway.adapters.postgres.files import FileRecord
from gateway.api.caller import AnyCaller, Caller, ModelCaller
from gateway.domain.errors import NotFound
from gateway.pipeline.files import FileService, Owner

router = APIRouter(tags=["data-plane"])


class FileServices(Protocol):
    @property
    def file_service(self) -> FileService: ...


def get_files(request: Request) -> FileService:
    services: FileServices = request.app.state.container
    return services.file_service


Files = Annotated[FileService, Depends(get_files)]


class FileOut(BaseModel):
    id: str
    object: Literal["file"] = "file"
    bytes: int
    created_at: int
    filename: str
    purpose: str
    status: Literal["processed"] = "processed"
    mime_type: str
    pages: int | None
    sha256: str

    @classmethod
    def of(cls, r: FileRecord) -> FileOut:
        return cls(
            id=f"file-{r.id.hex}",
            bytes=r.size,
            created_at=int(r.created_at.timestamp()),
            filename=r.filename,
            purpose=r.purpose,
            mime_type=r.mime,
            pages=r.pages,
            sha256=r.sha256.hex(),
        )


class FileList(BaseModel):
    object: Literal["list"] = "list"
    data: list[FileOut]
    has_more: bool
    first_id: str | None
    last_id: str | None


class FileDeleted(BaseModel):
    id: str
    object: Literal["file"] = "file"
    deleted: bool = True


def _owner(caller: Caller) -> Owner:
    return Owner(caller.org_id, caller.team_id, caller.user_id, caller.api_key_id)


def _file_id(raw: str) -> UUID:
    try:
        return UUID(raw.removeprefix("file-"))
    except ValueError:
        raise NotFound("no such file", code="file_not_found") from None


def _disposition(filename: str) -> str:
    fallback = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in filename
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.post("/v1/files")
async def upload_file(
    caller: ModelCaller,
    files: Files,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()] = "user_data",
) -> FileOut:
    return FileOut.of(await files.upload(_owner(caller), file.filename, file.file, purpose))


@router.get("/v1/files")
async def list_files(
    caller: AnyCaller,
    files: Files,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    after: str | None = None,
) -> FileList:
    records = await files.list(
        _owner(caller), limit=limit + 1, after=_file_id(after) if after else None
    )
    page = [FileOut.of(r) for r in records[:limit]]
    return FileList(
        data=page,
        has_more=len(records) > limit,
        first_id=page[0].id if page else None,
        last_id=page[-1].id if page else None,
    )


@router.get("/v1/files/{file_id}")
async def get_file(file_id: str, caller: AnyCaller, files: Files) -> FileOut:
    return FileOut.of(await files.get(_owner(caller), _file_id(file_id)))


@router.get("/v1/files/{file_id}/content")
async def file_content(file_id: str, caller: AnyCaller, files: Files) -> StreamingResponse:
    record, chunks = await files.content(_owner(caller), _file_id(file_id))
    return StreamingResponse(
        chunks,
        media_type=record.mime,
        headers={
            "Content-Disposition": _disposition(record.filename),
            "Content-Length": str(record.size),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/v1/files/{file_id}")
async def delete_file(file_id: str, caller: AnyCaller, files: Files) -> FileDeleted:
    parsed = _file_id(file_id)
    await files.delete(_owner(caller), parsed)
    return FileDeleted(id=f"file-{parsed.hex}")
