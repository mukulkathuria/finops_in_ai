"""Liveness and readiness against a real database (US-29)."""

import httpx2
from pydantic import SecretStr

from gateway.app.main import create_app
from gateway.app.settings import Settings
from tests.support.database import DisposableDatabase


async def _get(database_url: str, path: str) -> httpx2.Response:
    app = create_app(Settings(_env_file=None, database_url=SecretStr(database_url)))  # type: ignore[call-arg]
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


async def test_liveness_is_ok(test_db: DisposableDatabase) -> None:
    resp = await _get(test_db.app_url, "/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_ok_when_database_answers(test_db: DisposableDatabase) -> None:
    resp = await _get(test_db.app_url, "/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["database"] == "ok"


async def test_readiness_fails_cleanly_when_database_is_down() -> None:
    resp = await _get("postgresql://app:x@127.0.0.1:1/none", "/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "checks": {"database": "unavailable"}}
