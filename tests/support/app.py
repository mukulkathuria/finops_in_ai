"""In-process app clients for API tests (no network, real database)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx2
from fastapi import FastAPI
from mock_llm.app import app as mock_provider
from opentelemetry.sdk.trace.export import SpanExporter
from pydantic import SecretStr

from gateway.app.main import create_app
from gateway.app.settings import Settings
from tests.support.database import DisposableDatabase

STRONG_PASSWORD = "Correct-Horse-Battery-9!"


def app_settings(db: DisposableDatabase, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": SecretStr(db.app_url),
        "signup_mode": "open",
        "auth_rate_limit_per_minute": 100_000,  # tests share one client address
        "db_pool_size": 10,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def app_client(
    settings: Settings,
    *,
    client_ip: str = "127.0.0.1",
    span_exporter: SpanExporter | None = None,
) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    providers = httpx2.AsyncClient(
        mounts={"http://mock-llm:8000": httpx2.ASGITransport(app=mock_provider)}
    )
    app = create_app(
        settings,
        http_client=providers,
        environ={"MOCK_LLM_API_KEY": "test-key"},
        span_exporter=span_exporter,
    )
    async with providers, app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app, client=(client_ip, 50000))
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://gateway.test"
        ) as client:
            yield app, client


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}.{uuid4().hex[:12]}@example.com"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def signup(
    client: httpx2.AsyncClient, *, email: str | None = None, org_name: str | None = None
) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "org_name": org_name or f"Org {uuid4().hex[:8]}",
            "email": email or unique_email("admin"),
            "password": STRONG_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body
