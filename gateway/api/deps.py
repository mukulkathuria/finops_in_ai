"""Shared FastAPI dependencies: service access, authentication and authorization.

API modules depend on a structural `Protocol` of the services they use, never on the composition
root, so the data plane cannot import the control plane even indirectly (ADR 0001).

`require(permission)` is the authorization guard the route audit looks for. Authorization is deny
by default: a principal needs an explicit grant, and use cases re-check object scope with
`principal.can(permission, team_id)`.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request
from sqlalchemy.exc import SQLAlchemyError

from gateway.adapters.postgres.audit import AuditRepository
from gateway.adapters.postgres.database import Database, TenantContext
from gateway.adapters.postgres.identity import IdentityRepository, RoleCatalogCache
from gateway.adapters.security.tokens import TokenService
from gateway.api.markers import authz_dependency
from gateway.domain.audit import AuditAction, AuditEvent, RequestMeta
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
    @property
    def audit(self) -> AuditRepository: ...


log = logging.getLogger(__name__)


def get_services(request: Request) -> GatewayServices:
    services: GatewayServices = request.app.state.container
    return services


def request_meta(request: Request) -> RequestMeta:
    client = request.client.host if request.client else None
    return RequestMeta(ip=client, request_id=getattr(request.state, "request_id", None))


async def audit_denial(request: Request, principal: Principal, details: dict[str, object]) -> None:
    """Record an authorization denial in its own transaction. A failed write never grants access."""
    services = get_services(request)
    meta = request_meta(request)
    event = AuditEvent(
        action=AuditAction.ACCESS_DENIED,
        actor_type="user",
        actor_id=principal.user_id,
        target_type="route",
        target_id=f"{request.method} {request.url.path}",
        details=details,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    try:
        tenant = TenantContext(principal.org_id, principal.user_id)
        async with services.db.transaction(tenant) as conn:
            await services.audit.append(conn, event)
    except SQLAlchemyError:
        log.exception("could not audit an access denial", extra={"request_id": meta.request_id})


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
    async def guard(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        missing = [p.value for p in permissions if not principal.can_somewhere(p)]
        if missing:
            await audit_denial(request, principal, {"missing_permissions": missing})
            raise Forbidden("you do not have permission for this action")
        return principal

    return guard


@authz_dependency
async def require_user(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    """Self-service routes: any active user, acting only on their own resources."""
    return principal


AuthorizedPrincipal = Annotated[Principal, Depends(require_user)]
