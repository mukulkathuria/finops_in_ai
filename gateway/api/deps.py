"""Shared FastAPI dependencies: service access, authentication and authorization.

API modules depend on a structural `Protocol` of the services they use, never on the composition
root, so the data plane cannot import the control plane even indirectly (ADR 0001).

`require(permission)` is the authorization guard the route audit looks for. Authorization is deny
by default: a principal needs an explicit grant, and use cases re-check object scope with
`principal.can(permission, team_id)`.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request

from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.identity import IdentityRepository, RoleCatalogCache
from gateway.adapters.security.tokens import TokenService
from gateway.api.markers import authz_dependency
from gateway.domain.errors import Forbidden, Unauthenticated
from gateway.domain.policy import Permission, Principal


class GatewayServices(Protocol):
    @property
    def db(self) -> Database: ...
    @property
    def tokens(self) -> TokenService: ...
    @property
    def identity_repo(self) -> IdentityRepository: ...
    @property
    def role_catalog(self) -> RoleCatalogCache: ...


def get_services(request: Request) -> GatewayServices:
    services: GatewayServices = request.app.state.container
    return services


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("missing bearer token", code="missing_token")
    return token.strip()


async def current_principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    services = get_services(request)
    claims = services.tokens.verify_access(bearer_token(authorization))
    async with services.db.transaction(TenantContext(claims.org_id, claims.user_id)) as conn:
        user = await services.identity_repo.load_user(conn, claims.user_id)
        catalog = await services.role_catalog.get(conn)
    if user is None or user.status != "active":
        raise Unauthenticated("account is not active", code="account_inactive")
    return Principal(user.id, user.org_id, user.email, user.grants, catalog, user.team_ids)


def require(*permissions: Permission) -> Callable[..., Awaitable[Principal]]:
    """Route guard: the caller must hold every listed permission in at least one scope."""

    @authz_dependency
    async def guard(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not all(principal.can_somewhere(p) for p in permissions):
            raise Forbidden("you do not have permission for this action")
        return principal

    return guard


@authz_dependency
async def require_user(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """Self-service routes: any active user, acting only on their own resources."""
    return principal


AuthorizedPrincipal = Annotated[Principal, Depends(require_user)]
