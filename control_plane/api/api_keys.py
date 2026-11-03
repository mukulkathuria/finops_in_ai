"""Team API key endpoints (US-04). The full key appears only in the creation response."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import request_meta, require
from gateway.domain.policy import Permission, Principal

router = APIRouter(prefix="/v1/admin/teams/{team_id}/api-keys", tags=["api-keys"])
KeyManager = Annotated[Principal, Depends(require(Permission.API_KEYS_MANAGE))]


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    user_id: UUID | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyOut(BaseModel):
    id: UUID
    team_id: UUID
    user_id: UUID | None
    name: str
    prefix: str
    scopes: list[str]
    created_by: UUID
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class KeyCreated(KeyOut):
    key: str


class KeyList(BaseModel):
    data: list[KeyOut]


@router.post("", status_code=201)
async def create_key(
    team_id: UUID, body: KeyCreate, request: Request, principal: KeyManager, services: Services
) -> KeyCreated:
    created = await services.api_key_service.create(
        principal,
        team_id=team_id,
        name=body.name,
        user_id=body.user_id,
        expires_in_days=body.expires_in_days,
        meta=request_meta(request),
    )
    out = KeyOut.model_validate(created.record, from_attributes=True)
    return KeyCreated(**out.model_dump(), key=created.key)


@router.get("")
async def list_keys(
    team_id: UUID, request: Request, principal: KeyManager, services: Services
) -> KeyList:
    keys = await services.api_key_service.list(principal, team_id, request_meta(request))
    return KeyList(data=[KeyOut.model_validate(k, from_attributes=True) for k in keys])


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    team_id: UUID, key_id: UUID, request: Request, principal: KeyManager, services: Services
) -> None:
    await services.api_key_service.revoke(principal, team_id, key_id, request_meta(request))
