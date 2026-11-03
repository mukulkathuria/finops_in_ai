"""`GET /v1/whoami`: which org, team and credential a request authenticates as."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from gateway.api.caller import AnyCaller

router = APIRouter(tags=["data-plane"])


class WhoAmI(BaseModel):
    org_id: UUID
    team_id: UUID | None
    user_id: UUID | None
    api_key_id: UUID | None
    auth_method: str
    permissions: list[str]


@router.get("/v1/whoami")
async def whoami(caller: AnyCaller) -> WhoAmI:
    return WhoAmI(
        org_id=caller.org_id,
        team_id=caller.team_id,
        user_id=caller.user_id,
        api_key_id=caller.api_key_id,
        auth_method=caller.auth_method,
        permissions=sorted(caller.permissions),
    )
