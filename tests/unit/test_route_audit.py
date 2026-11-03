"""Every route must be @public or carry an authorization dependency (spec section 12)."""

from fastapi import APIRouter, Depends
from pydantic import SecretStr

from gateway.api.markers import authz_dependency
from gateway.app.main import create_app
from gateway.app.settings import Settings
from scripts.audit_routes import find_unmarked


def _app():  # type: ignore[no-untyped-def]
    return create_app(Settings(_env_file=None, database_url=SecretStr("postgresql://a:b@h/d")))  # type: ignore[call-arg]


def test_all_shipped_routes_are_marked() -> None:
    assert find_unmarked(_app()) == []


def test_unmarked_route_is_reported() -> None:
    app = _app()
    router = APIRouter()

    @router.get("/v1/leaky")
    async def leaky() -> dict[str, str]:
        return {}

    app.include_router(router)
    assert find_unmarked(app) == ["['GET'] /v1/leaky"]


def test_route_with_authz_dependency_passes() -> None:
    app = _app()
    router = APIRouter()

    @authz_dependency
    async def require_admin() -> None:
        return None

    @router.get("/v1/admin-only", dependencies=[Depends(require_admin)])
    async def admin_only() -> dict[str, str]:
        return {}

    app.include_router(router)
    assert find_unmarked(app) == []
