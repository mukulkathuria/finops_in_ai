"""Org alert webhook endpoints (spec F9). The signing secret is returned only when created or
rotated; the stored URL is never returned (only its host)."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.adapters.postgres.webhooks import WebhookEndpoint
from gateway.api.deps import AuthorizedPrincipal, request_meta

router = APIRouter(prefix="/v1/admin/webhook", tags=["webhooks"])


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class WebhookOut(BaseModel):
    url_hint: str
    created_at: datetime
    updated_at: datetime
    secret: str | None = None  # present only when the endpoint was created


class SecretOut(BaseModel):
    secret: str


class DeliveryOut(BaseModel):
    id: UUID
    topic: str
    created_at: datetime
    attempts: int
    delivered_at: datetime | None
    failed_at: datetime | None
    last_error: str | None


class DeliveryList(BaseModel):
    data: list[DeliveryOut]


def _out(endpoint: WebhookEndpoint, secret: str | None = None) -> WebhookOut:
    return WebhookOut(
        url_hint=endpoint.url_hint,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
        secret=secret,
    )


@router.put("")
async def set_webhook(
    body: WebhookIn,
    request: Request,
    response: Response,
    principal: AuthorizedPrincipal,
    services: Services,
) -> WebhookOut:
    endpoint, secret = await services.webhook_service.set_url(
        principal, body.url, request_meta(request)
    )
    response.status_code = 201 if secret else 200
    response.headers["cache-control"] = "no-store"
    return _out(endpoint, secret)


@router.get("")
async def get_webhook(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> WebhookOut:
    return _out(await services.webhook_service.get(principal, request_meta(request)))


@router.post("/secret")
async def rotate_secret(
    request: Request, response: Response, principal: AuthorizedPrincipal, services: Services
) -> SecretOut:
    response.headers["cache-control"] = "no-store"
    return SecretOut(
        secret=await services.webhook_service.rotate_secret(principal, request_meta(request))
    )


@router.delete("", status_code=204)
async def delete_webhook(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> None:
    await services.webhook_service.delete(principal, request_meta(request))


@router.get("/deliveries")
async def list_deliveries(
    request: Request, principal: AuthorizedPrincipal, services: Services
) -> DeliveryList:
    found = await services.webhook_service.deliveries(principal, request_meta(request))
    return DeliveryList(data=[DeliveryOut.model_validate(d, from_attributes=True) for d in found])
