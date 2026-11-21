"""Knowledge domains for graph retrieval (ADR 0011): `POST`, `GET` and `DELETE`
`/v1/admin/knowledge-domains`. Documents are filed under a domain with
`POST /v1/documents` (`knowledge_domain`)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.adapters.postgres.knowledge import DomainRecord
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.knowledge import DEFAULT_INTENT_THRESHOLD, DOMAIN_PATTERN

router = APIRouter(prefix="/v1/admin/knowledge-domains", tags=["retrieval"])

Question = Annotated[str, Field(min_length=1, max_length=1_000)]
TypeName = Annotated[str, Field(min_length=1, max_length=60)]


class DomainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=DOMAIN_PATTERN, description="Lower case, as `medical`.")
    description: str = Field(min_length=1, max_length=2_000)
    entity_types: list[TypeName] = Field(
        min_length=1, max_length=30, description="As disease, symptom, test."
    )
    relation_types: list[TypeName] = Field(
        min_length=1, max_length=30, description="As has_symptom, diagnosed_by."
    )
    examples: list[Question] = Field(
        min_length=1,
        max_length=50,
        description="Questions the graph should answer; their embeddings are the intent.",
    )
    intent_threshold: float = Field(default=DEFAULT_INTENT_THRESHOLD, ge=0, le=1)


class DomainOut(BaseModel):
    id: UUID
    object: Literal["knowledge_domain"] = "knowledge_domain"
    name: str
    description: str
    entity_types: list[str]
    relation_types: list[str]
    examples: list[str]
    intent_threshold: float
    embedding_model: str
    created_at: int

    @classmethod
    def of(cls, d: DomainRecord) -> DomainOut:
        return cls(
            id=d.id,
            name=d.name,
            description=d.description,
            entity_types=list(d.entity_types),
            relation_types=list(d.relation_types),
            examples=list(d.examples),
            intent_threshold=round(d.intent_threshold, 4),
            embedding_model=d.embedding_model,
            created_at=int(d.created_at.timestamp()),
        )


class DomainList(BaseModel):
    object: Literal["list"] = "list"
    data: list[DomainOut]


@router.post("")
async def create_knowledge_domain(
    body: DomainIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> DomainOut:
    record = await services.knowledge_service.create(
        principal,
        request_meta(request),
        name=body.name,
        description=body.description,
        entity_types=body.entity_types,
        relation_types=body.relation_types,
        examples=body.examples,
        threshold=body.intent_threshold,
    )
    return DomainOut.of(record)


@router.get("")
async def list_knowledge_domains(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> DomainList:
    records = await services.knowledge_service.domains(principal, request_meta(request))
    return DomainList(data=[DomainOut.of(r) for r in records])


@router.delete("/{domain_id}")
async def delete_knowledge_domain(
    domain_id: UUID, request: Request, principal: AuthorizedPrincipal, services: Services
) -> dict[str, object]:
    await services.knowledge_service.delete(principal, domain_id, request_meta(request))
    return {"id": str(domain_id), "object": "knowledge_domain", "deleted": True}
