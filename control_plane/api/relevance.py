"""Relevance filter settings (US-21): `GET` and `PUT /v1/admin/retrieval-settings`."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.retrieval import RelevanceSettings

router = APIRouter(prefix="/v1/admin/retrieval-settings", tags=["retrieval"])


class RelevanceOut(BaseModel):
    threshold: float
    max_tokens: int
    candidates: int


class RelevanceIn(BaseModel):
    """Any subset of the settings; omitted ones keep their current value."""

    model_config = ConfigDict(extra="forbid")
    threshold: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=100, le=200_000)
    candidates: int | None = Field(default=None, ge=1, le=100)


def _out(settings: RelevanceSettings) -> RelevanceOut:
    return RelevanceOut(**settings.to_dict())


@router.get("")
async def get_retrieval_settings(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> RelevanceOut:
    return _out(await services.relevance_service.get(principal, request_meta(request)))


@router.put("")
async def set_retrieval_settings(
    body: RelevanceIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> RelevanceOut:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    settings = await services.relevance_service.update(principal, changes, request_meta(request))
    return _out(settings)
