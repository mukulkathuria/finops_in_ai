"""Authentication endpoints (US-01, US-03) and the JWKS document.

Public endpoints are rate limited per client address; the credentials in their bodies (password,
refresh token) are the authentication.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from control_plane.services.sessions import Session
from gateway.api.deps import AuthorizedPrincipal, get_services, request_meta
from gateway.api.markers import public
from gateway.api.ratelimit import per_ip_limit

router = APIRouter(prefix="/v1/auth", tags=["auth"])
well_known = APIRouter(tags=["auth"])
auth_rate_limit = [Depends(per_ip_limit("auth"))]


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_name: str = Field(min_length=1, max_length=200)
    org_slug: str | None = Field(default=None, max_length=63)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=20, max_length=200)


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str


class UserOut(BaseModel):
    id: UUID
    email: str
    roles: list[str]


class SessionOut(BaseModel):
    org: OrgOut
    user: UserOut
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class MeOut(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    roles: list[str]


def session_out(session: Session) -> SessionOut:
    return SessionOut(
        org=OrgOut(id=session.org_id, name=session.org_name, slug=session.org_slug),
        user=UserOut(id=session.user_id, email=session.email, roles=list(session.roles)),
        access_token=session.access_token,
        expires_in=session.expires_in,
        refresh_token=session.refresh_token,
        refresh_expires_in=session.refresh_expires_in,
    )


@router.post("/signup", status_code=201, dependencies=auth_rate_limit)
@public
async def signup(body: SignupRequest, request: Request, services: Services) -> SessionOut:
    session = await services.identity_service.signup(
        org_name=body.org_name,
        org_slug=body.org_slug,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        meta=request_meta(request),
    )
    return session_out(session)


@router.post("/login", dependencies=auth_rate_limit)
@public
async def login(body: LoginRequest, request: Request, services: Services) -> SessionOut:
    session = await services.session_service.login(
        email=body.email, password=body.password, meta=request_meta(request)
    )
    return session_out(session)


@router.post("/refresh", dependencies=auth_rate_limit)
@public
async def refresh(body: RefreshRequest, request: Request, services: Services) -> SessionOut:
    session = await services.session_service.refresh(
        refresh_token=body.refresh_token, meta=request_meta(request)
    )
    return session_out(session)


@router.post("/logout", status_code=204, dependencies=auth_rate_limit)
@public
async def logout(body: RefreshRequest, request: Request, services: Services) -> None:
    await services.session_service.logout(
        refresh_token=body.refresh_token, meta=request_meta(request)
    )


@router.get("/me")
async def me(principal: AuthorizedPrincipal) -> MeOut:
    return MeOut(
        id=principal.user_id,
        org_id=principal.org_id,
        email=principal.email,
        roles=sorted(principal.roles),
    )


@well_known.get("/.well-known/jwks.json")
@public
async def jwks(request: Request) -> dict[str, list[dict[str, object]]]:
    return get_services(request).tokens.jwks()
