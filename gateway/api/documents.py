"""Documents for retrieval and the search over them (US-13).

`POST /v1/documents` indexes text, or a stored file converted to Markdown, shared with the org,
teams, users or roles (only org admins share beyond their own groups). `POST /v1/retrieval/search`
returns the nearest chunks the caller may see: the permission filter runs inside the vector
search itself. A chat request asks for retrieval with `"retrieval": {"k": 5}` (see openai.py).
"""

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from gateway.adapters.postgres.documents import DocumentRecord
from gateway.api.caller import AnyCaller, Caller, HeavyCaller, ModelCaller
from gateway.api.files import file_owner
from gateway.domain.errors import NotFound
from gateway.domain.policy import Permission
from gateway.domain.retrieval import Access, share_groups, team_group, user_group
from gateway.pipeline.parsing import file_id_of
from gateway.pipeline.retrieval import Author, RetrievalService

router = APIRouter(tags=["data-plane"])


class RetrievalServices(Protocol):
    @property
    def retrieval_service(self) -> RetrievalService: ...


def get_retrieval(request: Request) -> RetrievalService:
    services: RetrievalServices = request.app.state.container
    return services.retrieval_service


Retrieval = Annotated[RetrievalService, Depends(get_retrieval)]


class AccessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: bool = False
    teams: list[UUID] = Field(default_factory=list, max_length=100)
    users: list[UUID] = Field(default_factory=list, max_length=100)
    roles: list[str] = Field(default_factory=list, max_length=20)


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    text: str | None = Field(default=None, max_length=4_000_000)
    file_id: str | None = None
    access: AccessIn = Field(default_factory=AccessIn)


class DocumentOut(BaseModel):
    id: UUID
    object: Literal["document"] = "document"
    title: str
    file_id: str | None
    access: list[str]
    chunks: int
    tokens: int
    embedding_model: str
    created_at: int
    warnings: list[str] = Field(default_factory=list)  # what the index left out, and why

    @classmethod
    def of(cls, d: DocumentRecord, warnings: Sequence[str] = ()) -> DocumentOut:
        return cls(
            id=d.id,
            title=d.title,
            file_id=f"file-{d.file_id.hex}" if d.file_id else None,
            access=d.acl_groups,
            chunks=d.chunk_count,
            tokens=d.token_count,
            embedding_model=d.embedding_model,
            created_at=int(d.created_at.timestamp()),
            warnings=list(warnings),
        )


class DocumentList(BaseModel):
    object: Literal["list"] = "list"
    data: list[DocumentOut]


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=8_000)
    k: int = Field(default=5, ge=1, le=50)


class HitOut(BaseModel):
    chunk_id: UUID
    document_id: UUID
    title: str
    content: str
    score: float


class SearchOut(BaseModel):
    object: Literal["list"] = "list"
    data: list[HitOut]


def _default_group(caller: Caller) -> str:
    if caller.user_id is not None:
        return user_group(caller.user_id)
    if caller.team_id is not None:
        return team_group(caller.team_id)
    raise NotFound("this credential has no user or team to own a document")


@router.post("/v1/documents")
async def create_document(
    body: DocumentIn, caller: HeavyCaller, retrieval: Retrieval
) -> DocumentOut:
    access = Access(
        org=body.access.org,
        teams=tuple(body.access.teams),
        users=tuple(body.access.users),
        roles=tuple(body.access.roles),
    )
    groups = share_groups(
        access,
        own=caller.groups,
        default=_default_group(caller),
        admin=caller.can(Permission.ORG_MANAGE),
    )
    file_id = file_id_of(body.file_id) if body.file_id is not None else None
    ingested = await retrieval.ingest(
        Author(file_owner(caller), groups), body.title, body.text, file_id
    )
    return DocumentOut.of(ingested.record, ingested.warnings)


@router.get("/v1/documents")
async def list_documents(
    caller: AnyCaller,
    retrieval: Retrieval,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> DocumentList:
    records = await retrieval.documents(file_owner(caller), caller.groups, limit=limit)
    return DocumentList(data=[DocumentOut.of(r) for r in records])


@router.delete("/v1/documents/{document_id}")
async def delete_document(
    document_id: UUID, caller: ModelCaller, retrieval: Retrieval
) -> dict[str, object]:
    admin = caller.can(Permission.ORG_MANAGE)
    await retrieval.delete(file_owner(caller), document_id, admin=admin)
    return {"id": str(document_id), "object": "document", "deleted": True}


@router.post("/v1/retrieval/search")
async def search(body: SearchIn, caller: ModelCaller, retrieval: Retrieval) -> SearchOut:
    hits = await retrieval.search(file_owner(caller), caller.groups, body.query, body.k)
    return SearchOut(
        data=[
            HitOut(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                title=h.title,
                content=h.content,
                score=round(h.score, 4),
            )
            for h in hits
        ]
    )
