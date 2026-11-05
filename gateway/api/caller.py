"""Data-plane authentication: a team API key or a user JWT becomes a `Caller` (US-04, US-05).

API keys are looked up by digest on every request with no cache, so revocation is immediate.
JWT callers choose a team with `X-FinOps-Team` (required when they belong to several teams).
"""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import Depends, Header, Request

from gateway.adapters.observability.telemetry import Telemetry
from gateway.adapters.postgres.api_keys import ApiKeyRepository
from gateway.adapters.postgres.database import TenantContext, set_lookup, set_tenant
from gateway.api.deps import GatewayServices, bearer_token
from gateway.api.markers import authz_dependency
from gateway.app.settings import Settings
from gateway.domain.api_keys import api_key_digest, looks_like_api_key
from gateway.domain.errors import Forbidden, InvalidInput, RateLimited, Unauthenticated
from gateway.domain.policy import Permission
from gateway.domain.retrieval import caller_groups
from gateway.ports.counters import CounterStore


class CallerServices(GatewayServices, Protocol):
    @property
    def api_keys(self) -> ApiKeyRepository: ...
    @property
    def telemetry(self) -> Telemetry: ...
    @property
    def counters(self) -> CounterStore: ...
    @property
    def settings(self) -> Settings: ...


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
    # A JWT user in several teams who sent no X-FinOps-Team: fine for self-inspection, but a
    # model call must say which team pays (team and member budgets key on the team).
    team_ambiguous: bool = False
    # Who the caller is for document permissions (US-13): the org, itself, its teams, its
    # org-level roles. A team key is its team (and its bound user), nothing more.
    groups: frozenset[str] = frozenset()

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
        groups=caller_groups(user_id=identity.user_id, team_ids=(identity.team_id,), roles=()),
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
        team_ambiguous=team_id is None and len(user.team_ids) > 1,
        groups=caller_groups(
            user_id=user.id,
            team_ids=user.team_ids,
            roles=(g.role for g in user.grants if g.team_id is None),
        ),
    )


async def current_caller(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_finops_team: Annotated[str | None, Header()] = None,
) -> Caller:
    services = get_caller_services(request)
    token = bearer_token(authorization)
    with services.telemetry.stage("auth"):
        caller = (
            await _from_api_key(services, token)
            if looks_like_api_key(token)
            else await _from_jwt(services, token, x_finops_team)
        )
    request.state.caller = caller
    await _count(services, "credential", caller, services.settings.rate_limit_per_minute)
    return caller


async def _count(services: CallerServices, bucket: str, caller: Caller, limit: int) -> None:
    """One request against the credential's per-minute limit for `bucket` (spec section 10)."""
    decision = await services.counters.hit(
        f"{bucket}:{caller.api_key_id or caller.user_id}", limit=limit, window_s=60
    )
    if not decision.allowed:
        raise RateLimited(
            "too many requests for this credential; slow down",
            retry_after_s=decision.retry_after_s,
            code="rate_limited",
        )


@authz_dependency
async def require_caller(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    """Any authenticated caller, for self-inspection only."""
    return caller


@authz_dependency
async def require_model_access(caller: Annotated[Caller, Depends(current_caller)]) -> Caller:
    if not caller.can(Permission.MODELS_USE):
        raise Forbidden("this credential may not call models", code="forbidden")
    if caller.team_ambiguous:
        raise InvalidInput(
            "you belong to several teams: send X-FinOps-Team with the id of the team that pays",
            code="team_required",
            param="X-FinOps-Team",
        )
    return caller


async def count_heavy(request: Request, caller: Caller) -> None:
    """One request against the credential's separate limit on conversion or embedding work no
    money budget covers: uploads, document ingests, Markdown conversions, workspace facts."""
    services = get_caller_services(request)
    await _count(services, "heavy", caller, services.settings.heavy_rate_limit_per_minute)


@authz_dependency
async def require_heavy_access(
    request: Request, caller: Annotated[Caller, Depends(require_model_access)]
) -> Caller:
    """Model access, within the credential's limit on heavy work (`count_heavy`)."""
    await count_heavy(request, caller)
    return caller


AnyCaller = Annotated[Caller, Depends(require_caller)]
ModelCaller = Annotated[Caller, Depends(require_model_access)]
HeavyCaller = Annotated[Caller, Depends(require_heavy_access)]
