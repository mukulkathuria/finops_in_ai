"""Invitation endpoints (US-02). Acceptance is public: the link's secret token is the credential."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.auth import OrgOut, SessionOut, UserOut
from control_plane.api.deps import Services
from control_plane.services.invitations import OrgRole
from gateway.api.deps import AuthorizedPrincipal, request_meta, require
from gateway.api.markers import public
from gateway.domain.policy import Permission, Principal
from gateway.domain.teams import TeamRole

router = APIRouter(prefix="/v1/admin/invitations", tags=["invitations"])
accept_router = APIRouter(prefix="/v1/auth", tags=["auth"])


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    org_role: OrgRole = "member"
    team_id: UUID | None = None
    team_role: TeamRole | None = None


class InvitationOut(BaseModel):
    id: UUID
    email: str
    org_role: str
    team_id: UUID | None
    team_role: str | None
    expires_at: datetime
    created_at: datetime


class InvitationCreated(InvitationOut):
    invite_url: str
    token: str


class InvitationList(BaseModel):
    data: list[InvitationOut]


class AcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(default="", max_length=200)


@router.post("", status_code=201)
async def invite(
    body: InvitationCreate, request: Request, principal: AuthorizedPrincipal, services: Services
) -> InvitationCreated:
    created = await services.invitation_service.invite(
        principal,
        email=body.email,
        org_role=body.org_role,
        team_id=body.team_id,
        team_role=body.team_role,
        meta=request_meta(request),
    )
    out = InvitationOut.model_validate(created.invitation, from_attributes=True)
    return InvitationCreated(**out.model_dump(), invite_url=created.invite_url, token=created.token)


@router.get("")
async def list_invitations(principal: AuthorizedPrincipal, services: Services) -> InvitationList:
    pending = await services.invitation_service.list_pending(principal)
    return InvitationList(
        data=[InvitationOut.model_validate(i, from_attributes=True) for i in pending]
    )


@router.delete("/{invitation_id}", status_code=204)
async def revoke(
    invitation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.TEAM_MEMBERS_MANAGE))],
    services: Services,
) -> None:
    await services.invitation_service.revoke(principal, invitation_id, request_meta(request))


@accept_router.post("/invitations/accept", status_code=201)
@public
async def accept(body: AcceptRequest, request: Request, services: Services) -> SessionOut:
    result = await services.invitation_service.accept(
        token=body.token,
        password=body.password,
        display_name=body.display_name,
        meta=request_meta(request),
    )
    return SessionOut(
        org=OrgOut(id=result.org_id, name=result.org_name, slug=result.org_slug),
        user=UserOut(id=result.user_id, email=result.email, roles=list(result.roles)),
        access_token=result.access_token,
        expires_in=result.expires_in,
    )
