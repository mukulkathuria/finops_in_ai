"""Route guards and error mapping without a database (deny by default, OpenAI error shape)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid7

import httpx2
import pytest
from fastapi import Depends, FastAPI

from gateway.api.deps import current_principal, require
from gateway.api.errors import install_error_handlers
from gateway.domain.audit import AuditEvent
from gateway.domain.errors import RateLimited
from gateway.domain.policy import Permission, Principal, Role, RoleGrant

CATALOG = {"org_admin": frozenset({"teams:manage"}), "member": frozenset({"models:use"})}


class _FakeDb:
    @asynccontextmanager
    async def transaction(self, ctx: object = None) -> AsyncIterator[object]:
        yield object()


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, conn: object, event: AuditEvent) -> int:
        self.events.append(event)
        return len(self.events)


class _FakeServices:
    def __init__(self) -> None:
        self.db = _FakeDb()
        self.audit = _FakeAudit()


def _app(principal: Principal) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.state.container = _FakeServices()

    @app.get("/teams", dependencies=[Depends(require(Permission.TEAMS_MANAGE))])
    async def teams() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("secret internal detail")

    @app.get("/slow-down")
    async def slow_down() -> dict[str, str]:
        raise RateLimited("too many requests", retry_after_s=7)

    @app.get("/who")
    async def who(p: Annotated[Principal, Depends(current_principal)]) -> dict[str, str]:
        return {"email": p.email}

    app.dependency_overrides[current_principal] = lambda: principal
    return app


def _principal(*roles: str) -> Principal:
    return Principal(uuid7(), uuid7(), "u@example.com", tuple(RoleGrant(r) for r in roles), CATALOG)


@pytest.fixture
def clients() -> tuple[FastAPI, FastAPI]:
    return _app(_principal(Role.ORG_ADMIN)), _app(_principal(Role.MEMBER))


async def _get(app: FastAPI, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_guard_allows_granted_permission(clients: tuple[FastAPI, FastAPI]) -> None:
    admin, _ = clients
    assert (await _get(admin, "/teams")).json() == {"ok": "yes"}
    assert admin.state.container.audit.events == []


async def test_guard_denies_by_default_and_audits_the_denial(
    clients: tuple[FastAPI, FastAPI],
) -> None:
    _, member = clients
    resp = await _get(member, "/teams")
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "message": "you do not have permission for this action",
        "type": "permission_error",
        "code": "forbidden",
        "param": None,
    }
    [event] = member.state.container.audit.events
    assert event.action == "authz.access_denied"
    assert event.target_id == "GET /teams"
    assert dict(event.details) == {"missing_permissions": ["teams:manage"]}


async def test_unexpected_errors_are_generic_500s(clients: tuple[FastAPI, FastAPI]) -> None:
    admin, _ = clients
    resp = await _get(admin, "/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in resp.text


async def test_rate_limits_carry_retry_after(clients: tuple[FastAPI, FastAPI]) -> None:
    admin, _ = clients
    resp = await _get(admin, "/slow-down")
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "7"
    assert resp.json()["error"]["type"] == "rate_limit_error"


async def test_unknown_routes_use_the_error_shape(clients: tuple[FastAPI, FastAPI]) -> None:
    admin, _ = clients
    resp = await _get(admin, "/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"


def test_audit_events_refuse_secret_or_prompt_fields() -> None:
    for key in ("password", "api_key", "prompt_text", "Authorization"):
        with pytest.raises(ValueError, match="must not carry"):
            AuditEvent(action="auth.signup", actor_type="user", details={key: "x"})
    with pytest.raises(ValueError, match="must not carry"):
        AuditEvent(action="auth.signup", actor_type="user", details={"nested": {"secret": "x"}})
    assert AuditEvent(action="auth.signup", actor_type="user", details={"org_slug": "a"}).details
