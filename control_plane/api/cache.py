"""Response cache settings endpoints (US-18): `GET` and `PUT /v1/admin/cache-settings`."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, request_meta
from gateway.domain.cache import CacheSettings

router = APIRouter(prefix="/v1/admin/cache-settings", tags=["cache"])


class CacheSettingsOut(BaseModel):
    mode: str
    ttl_s: int
    threshold: float


class CacheSettingsIn(BaseModel):
    """Any subset of the settings; omitted ones keep their current value."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "exact", "semantic"] | None = None
    ttl_s: int | None = Field(default=None, ge=60, le=30 * 86_400)
    threshold: float | None = Field(default=None, ge=0.8, le=1.0)


def _out(settings: CacheSettings) -> CacheSettingsOut:
    return CacheSettingsOut(**settings.to_dict())


@router.get("")
async def get_cache_settings(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> CacheSettingsOut:
    return _out(await services.cache_settings_service.get(principal, request_meta(request)))


@router.put("")
async def set_cache_settings(
    body: CacheSettingsIn, request: Request, principal: AuthorizedPrincipal, services: Services
) -> CacheSettingsOut:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    settings = await services.cache_settings_service.update(
        principal, changes, request_meta(request)
    )
    return _out(settings)
