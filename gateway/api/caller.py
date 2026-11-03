"""Data-plane authentication: a team API key or a user JWT becomes a `Caller` (US-04, US-05).

API keys are looked up by digest on every request with no cache, so revocation is immediate.
JWT callers choose a team with `X-FinOps-Team` (required when they belong to several teams).
"""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import Depends, Header, Request

from gateway.adapters.postgres.api_keys import ApiKeyRepository
from gateway.adapters.postgres.database import TenantContext, set_lookup, set_tenant
from gateway.api.deps import GatewayServices, bearer_token
from gateway.api.markers import authz_dependency
from gateway.domain.api_keys import api_key_digest, looks_like_api_key
from gateway.domain.errors import Forbidden, Unauthenticated
from gateway.domain.policy import Permission


class CallerServices(GatewayServices, Protocol):
    @property
    def api_keys(self) -> ApiKeyRepository: ...


def get_caller_services(request: Request) -> CallerServices:
    services: CallerServices = request.app.state.container
    return services


@dataclass(frozen=True, slots=True)
class Caller:
    org_id: UUID
    user_id: UUID | None
    team_id: UUID | None
    api_key_id: UUID | None
    permissions: frozenset[str]
    auth_method: Literal["api_key", "jwt"]

    def can(self, permission: Permission) -> bool:
        return permission.value in self.permissions


_BAD_KEY = Unauthenticated("API key is not valid or has been revoked", code="invalid_api_key")


async def _from_api_key(services: CallerServices, raw: str) -> Caller:
    key_hash = api_key_digest(raw)
    if key_hash is None:
        raise _BAD_KEY
    async with services.db.transaction() as conn:
        await set_lookup(conn, "key_hash", key_hash)
        identity = await services.api_keys.authenticate(conn, key_hash)
        if identity is None:
            raise _BAD_KEY
        await set_tenant(conn, TenantContext(identity.org_id, identity.user_id))
        if identity.user_id is not None and not await services.api_keys.bound_member_is_active(
            conn, team_id=identity.team_id, user_id=identity.user_id
        ):
            raise _BAD_KEY
        await services.api_keys.touch(conn, identity.key_id)
    return Caller(
        org_id=identity.org_id,
        user_id=identity.user_id,
        team_id=identity.team_id,
        api_key_id=identity.key_id,
        permissions=identity.scopes,
        auth_method="api_key",
    )


async def _from_jwt(services: CallerServices, token: str, team_header: str | None) -> Caller:
    claims = services.tokens.verify_access(token)
    async with services.db.transaction(TenantContext(claims.org_id, claims.user_id)) as conn:
        user = await services.identity_repo.load_user(conn, claims.user_id)
        catalog = await services.role_catalog.get(conn)
    if user is None or user.status != "active":
        raise Unauthenticated("account is not active", code="account_inactive")
    permissions = frozenset(p for g in user.grants for p in catalog.get(g.role, frozenset()))
    team_id: UUID | None = None
    if team_header:
        try:
            team_id = UUID(team_header)
        except ValueError as exc:
            raise Forbidden("X-FinOps-Team is not a valid team id", code="invalid_team") from exc
        if team_id not in user.team_ids:
            raise Forbidden("you are not a member of that team", code="not_a_team_member")
    elif len(user.team_ids) == 1:
        (team_id,) = user.team_ids
    return Caller(
        org_id=user.org_id,
        user_id=user.id,
        team_id=team_id,
        api_key_id=None,
        permissions=permissions,
        auth_method="jwt",
    )


async def current_caller(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_finops_team: Annotated[str | None, Header()] = None,
) -> Caller:
    services = get_caller_services(request)
    token = bearer_token(authorization)
    caller = (
        await _from_api_key(services, token)
        if looks_like_api_key(token)
        else await _from_jwt(services, token, x_finops_team)
    )
    request.state.caller = caller
    return caller


@authz_dependency
async def require_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    """Any authenticated caller, for self-inspection only."""
    return caller


@authz_dependency
async def require_model_access(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    if not caller.can(Permission.MODELS_USE):
        raise Forbidden("this credential may not call models", code="forbidden")
    return caller


AnyCaller = Annotated[Caller, Depends(require_caller)]
ModelCaller = Annotated[Caller, Depends(require_model_access)]
