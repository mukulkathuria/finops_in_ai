"""Authentication endpoints (US-01) and the JWKS document."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, get_services
from gateway.api.markers import public

router = APIRouter(prefix="/v1/auth", tags=["auth"])
well_known = APIRouter(tags=["auth"])


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_name: str = Field(min_length=1, max_length=200)
    org_slug: str | None = Field(default=None, max_length=63)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(default="", max_length=200)


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


class MeOut(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    roles: list[str]


@router.post("/signup", status_code=201)
@public
async def signup(body: SignupRequest, services: Services) -> SessionOut:
    result = await services.identity_service.signup(
        org_name=body.org_name,
        org_slug=body.org_slug,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
    )
    return SessionOut(
        org=OrgOut(id=result.org_id, name=result.org_name, slug=result.org_slug),
        user=UserOut(id=result.user_id, email=result.email, roles=list(result.roles)),
        access_token=result.access_token,
        expires_in=result.expires_in,
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
