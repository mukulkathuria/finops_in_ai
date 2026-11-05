"""Single sign-on (US-27): the browser flow through the org's OpenID Connect provider, and its
configuration by admins.

The start, callback and exchange are public: the org's slug, the state with the provider's code,
and the one-time code are what they act on. Each is rate limited per client address, and none
is cached. The client secret goes in and never comes out."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.auth import SessionOut, auth_rate_limit, session_out
from control_plane.api.deps import Services
from control_plane.services.sso import SsoChange, SsoSettings
from gateway.api.deps import request_meta, require
from gateway.api.markers import public
from gateway.domain.policy import Permission, Principal
from gateway.domain.sso import STATE_TTL_S

router = APIRouter(prefix="/v1/auth/sso", tags=["auth"])
admin_router = APIRouter(prefix="/v1/admin/sso", tags=["sso"])
Admin = Annotated[Principal, Depends(require(Permission.ORG_MANAGE))]
_NO_STORE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}
# The browser that started a sign-in keeps this secret. The provider's redirect back is a
# top-level GET, which a SameSite=Lax cookie still accompanies.
_BROWSER = "finops_sso"


def _cookie(services: Services) -> dict[str, Any]:
    return {
        "path": "/v1/auth/sso",
        "secure": services.sso_service.secure_cookies,
        "httponly": True,
        "samesite": "lax",
    }


class SsoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issuer: str = Field(min_length=1, max_length=500)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str | None = Field(
        default=None, max_length=1000, description="Left out keeps the stored one; empty: none."
    )
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    local_login_disabled: bool = False


class SsoOut(BaseModel):
    issuer: str
    client_id: str
    confidential_client: bool
    allowed_domains: list[str]
    local_login_disabled: bool
    callback_url: str
    updated_at: datetime

    @classmethod
    def of(cls, s: SsoSettings) -> SsoOut:
        return cls(
            issuer=s.issuer,
            client_id=s.client_id,
            confidential_client=s.confidential_client,
            allowed_domains=list(s.allowed_domains),
            local_login_disabled=s.local_login_disabled,
            callback_url=s.callback_url,
            updated_at=s.updated_at,
        )


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=200)


@router.get("/start", dependencies=auth_rate_limit)
@public
async def start(
    services: Services, org: Annotated[str, Query(min_length=1, max_length=63)]
) -> RedirectResponse:
    url, browser = await services.sso_service.start(org)
    response = RedirectResponse(url, status_code=303, headers=_NO_STORE)
    response.set_cookie(_BROWSER, browser, max_age=STATE_TTL_S, **_cookie(services))
    return response


@router.get("/callback", dependencies=auth_rate_limit)
@public
async def callback(
    request: Request,
    services: Services,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=200)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    url = await services.sso_service.callback(
        code=code,
        state=state,
        error=error,
        browser=request.cookies.get(_BROWSER, ""),
        meta=request_meta(request),
    )
    return RedirectResponse(url, status_code=303, headers=_NO_STORE)


@router.post("/exchange", dependencies=auth_rate_limit)
@public
async def exchange(
    body: ExchangeIn, request: Request, response: Response, services: Services
) -> SessionOut:
    browser = request.cookies.get(_BROWSER, "")
    session = await services.sso_service.exchange(body.code, browser, request_meta(request))
    response.delete_cookie(_BROWSER, **_cookie(services))
    return session_out(session)


@admin_router.get("")
async def get_sso(request: Request, principal: Admin, services: Services) -> SsoOut:
    return SsoOut.of(await services.sso_service.settings(principal, request_meta(request)))


@admin_router.put("")
async def set_sso(body: SsoIn, request: Request, principal: Admin, services: Services) -> SsoOut:
    change = SsoChange(
        issuer=body.issuer,
        client_id=body.client_id,
        client_secret=body.client_secret,
        allowed_domains=tuple(body.allowed_domains),
        local_login_disabled=body.local_login_disabled,
    )
    return SsoOut.of(await services.sso_service.configure(principal, change, request_meta(request)))


@admin_router.delete("", status_code=204)
async def delete_sso(request: Request, principal: Admin, services: Services) -> None:
    await services.sso_service.remove(principal, request_meta(request))
