"""Provider key endpoints (US-06). Keys go in; only a 4-character hint ever comes out."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import request_meta, require
from gateway.domain.policy import Permission, Principal

router = APIRouter(prefix="/v1/admin/providers", tags=["providers"])
Manager = Annotated[Principal, Depends(require(Permission.PROVIDERS_MANAGE))]
ProviderName = Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,40}$")]


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=1024)
    label: str = Field(default="", max_length=100)


class CredentialOut(BaseModel):
    id: UUID
    provider: str
    label: str
    key_hint: str
    created_at: datetime


class CredentialList(BaseModel):
    data: list[CredentialOut]


@router.put("/{provider}/key", status_code=201)
async def set_key(
    provider: ProviderName, body: KeyIn, request: Request, principal: Manager, services: Services
) -> CredentialOut:
    info = await services.provider_key_service.add(
        principal, provider, body.api_key, body.label, request_meta(request)
    )
    return CredentialOut.model_validate(info, from_attributes=True)


@router.get("")
async def list_keys(request: Request, principal: Manager, services: Services) -> CredentialList:
    items = await services.provider_key_service.list(principal, request_meta(request))
    return CredentialList(
        data=[CredentialOut.model_validate(i, from_attributes=True) for i in items]
    )


@router.delete("/{provider}/key", status_code=204)
async def delete_key(
    provider: ProviderName, request: Request, principal: Manager, services: Services
) -> None:
    await services.provider_key_service.delete(principal, provider, request_meta(request))
