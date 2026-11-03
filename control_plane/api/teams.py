"""Teams and membership endpoints (US-02)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.deps import Services
from gateway.api.deps import AuthorizedPrincipal, request_meta, require
from gateway.domain.policy import Permission, Principal
from gateway.domain.teams import TeamRole

router = APIRouter(prefix="/v1/admin/teams", tags=["teams"])


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class TeamOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    member_count: int


class TeamList(BaseModel):
    data: list[TeamOut]


class MemberAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    role: TeamRole = "member"


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    is_lead: bool


class MemberList(BaseModel):
    data: list[MemberOut]


@router.post("", status_code=201)
async def create_team(
    body: TeamCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.TEAMS_MANAGE))],
    services: Services,
) -> TeamOut:
    team = await services.team_service.create_team(principal, body.name, request_meta(request))
    return TeamOut.model_validate(team, from_attributes=True)


@router.get("")
async def list_teams(principal: AuthorizedPrincipal, services: Services) -> TeamList:
    teams = await services.team_service.list_teams(principal)
    return TeamList(data=[TeamOut.model_validate(t, from_attributes=True) for t in teams])


@router.get("/{team_id}/members")
async def list_members(
    team_id: UUID, request: Request, principal: AuthorizedPrincipal, services: Services
) -> MemberList:
    members = await services.team_service.list_members(principal, team_id, request_meta(request))
    return MemberList(data=[MemberOut.model_validate(m, from_attributes=True) for m in members])


@router.post("/{team_id}/members", status_code=201)
async def add_member(
    team_id: UUID,
    body: MemberAdd,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require(Permission.TEAM_MEMBERS_MANAGE))],
    services: Services,
) -> dict[str, object]:
    added = await services.team_service.add_member(
        principal, team_id, body.user_id, body.role, request_meta(request)
    )
    response.status_code = 201 if added else 200
    return {"team_id": str(team_id), "user_id": str(body.user_id), "role": body.role}


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_member(
    team_id: UUID,
    user_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require(Permission.TEAM_MEMBERS_MANAGE))],
    services: Services,
) -> None:
    await services.team_service.remove_member(principal, team_id, user_id, request_meta(request))
