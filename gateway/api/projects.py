"""Code projects (US-22): `POST`/`GET /v1/projects`, `POST /v1/projects/{id}/index` (from an
uploaded source bundle or Graphify `graph.json`) and `GET /v1/projects/{id}/context?q=` (the
context packs of the modules a question names). A project is shared like a document; its graph
and packs inherit that ACL."""

from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from gateway.adapters.postgres.graph import ProjectRecord
from gateway.api.caller import AnyCaller, HeavyCaller, ModelCaller
from gateway.api.documents import AccessIn, default_group
from gateway.api.files import file_owner
from gateway.domain.policy import Permission
from gateway.domain.retrieval import Access, share_groups
from gateway.pipeline.code import CodeContext, CodeService, IndexReport
from gateway.pipeline.parsing import file_id_of

router = APIRouter(tags=["data-plane"])


class CodeServices(Protocol):
    @property
    def code_service(self) -> CodeService: ...


def get_code(request: Request) -> CodeService:
    services: CodeServices = request.app.state.container
    return services.code_service


Code = Annotated[CodeService, Depends(get_code)]


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    access: AccessIn = Field(default_factory=AccessIn)


class ProjectOut(BaseModel):
    id: str
    object: Literal["project"] = "project"
    name: str
    access: list[str]
    indexed_at: int | None
    created_at: int

    @classmethod
    def of(cls, p: ProjectRecord) -> ProjectOut:
        return cls(
            id=str(p.id),
            name=p.name,
            access=sorted(p.acl_groups),
            indexed_at=_epoch(p.indexed_at),
            created_at=int(p.created_at.timestamp()),
        )


class ProjectList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ProjectOut]


class IndexIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str = Field(min_length=1, max_length=100)


class IndexOut(BaseModel):
    object: Literal["code_index"] = "code_index"
    project_id: str
    source: str
    modules: int
    symbols: int
    edges: int
    packs: int
    skipped: list[str]
    skipped_links: int

    @classmethod
    def of(cls, project_id: UUID, r: IndexReport) -> IndexOut:
        return cls(
            project_id=str(project_id),
            source=r.source,
            modules=r.modules,
            symbols=r.symbols,
            edges=r.edges,
            packs=r.packs,
            skipped=list(r.skipped),
            skipped_links=r.skipped_links,
        )


class PackOut(BaseModel):
    path: str
    tokens: int
    source_tokens: int
    text: str


class ContextOut(BaseModel):
    object: Literal["code_context"] = "code_context"
    project_id: str
    packs: list[PackOut]
    symbols: list[str]
    tokens: int
    source_tokens: int

    @classmethod
    def of(cls, project_id: UUID, c: CodeContext) -> ContextOut:
        packs = [
            PackOut(path=p.path, tokens=p.tokens, source_tokens=p.source_tokens, text=p.text)
            for p in c.packs
        ]
        return cls(
            project_id=str(project_id),
            packs=packs,
            symbols=list(c.symbols),
            tokens=c.tokens,
            source_tokens=c.source_tokens,
        )


def _epoch(at: datetime | None) -> int | None:
    return int(at.timestamp()) if at is not None else None


@router.post("/v1/projects")
async def create_project(body: ProjectIn, caller: ModelCaller, code: Code) -> ProjectOut:
    access = Access(
        org=body.access.org,
        teams=tuple(body.access.teams),
        users=tuple(body.access.users),
        roles=tuple(body.access.roles),
    )
    groups = share_groups(
        access,
        own=caller.groups,
        default=default_group(caller),
        admin=caller.can(Permission.ORG_MANAGE),
    )
    project = await code.create(file_owner(caller), body.name, groups)
    return ProjectOut.of(project)


@router.get("/v1/projects")
async def list_projects(
    caller: AnyCaller, code: Code, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> ProjectList:
    projects = await code.projects(file_owner(caller), caller.groups, limit=limit)
    return ProjectList(data=[ProjectOut.of(p) for p in projects])


@router.post("/v1/projects/{project_id}/index")
async def index_project(
    project_id: UUID, body: IndexIn, caller: HeavyCaller, code: Code
) -> IndexOut:
    report = await code.index(
        file_owner(caller),
        caller.groups,
        project_id,
        file_id_of(body.file_id),
        admin=caller.can(Permission.ORG_MANAGE),
    )
    return IndexOut.of(project_id, report)


@router.get("/v1/projects/{project_id}/context")
async def project_context(
    project_id: UUID,
    caller: ModelCaller,
    code: Code,
    q: Annotated[str, Query(min_length=1, max_length=4_000)],
    max_tokens: Annotated[int, Query(ge=100, le=100_000)] = 4_000,
) -> ContextOut:
    context = await code.context(
        file_owner(caller), caller.groups, project_id, q, max_tokens=max_tokens
    )
    return ContextOut.of(project_id, context)
