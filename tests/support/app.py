"""In-process app clients for API tests (no network, real database)."""

import tempfile
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
from tests.support import fake_docparse, fake_idp
from tests.support.database import DisposableDatabase

STRONG_PASSWORD = "Correct-Horse-Battery-9!"
OBJECTS = tempfile.mkdtemp(prefix="finops-objects-")  # one per test process


def app_settings(db: DisposableDatabase, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": SecretStr(db.app_url),
        "signup_mode": "open",
        "auth_rate_limit_per_minute": 100_000,  # tests share one client address
        "rate_limit_per_minute": 1_000_000,  # and reuse a key for many requests a minute
        "heavy_rate_limit_per_minute": 1_000_000,
        "db_pool_size": 10,
        "laya_base_url": "http://mock-llm:8000",  # the mock provider speaks SystemOne too
        "object_store_path": OBJECTS,
        "glm_ocr_base_url": "http://docparse:8000/v1",
        "paddleocr_vl_base_url": "http://docparse:8000/v1",
        "parser_base_url": "http://docparse:8000/v1",
        "embedder_base_url": "http://mock-llm:8000/v1",
        "embedder_api_key": SecretStr("test-key"),
        "reranker_base_url": "http://mock-llm:8000/v1",
        "reranker_api_key": SecretStr("test-key"),
        "log_level": "WARNING",
        "public_base_url": "http://gateway.test",
    }
    values.update(overrides)
    return Settings(**values)


class _Recording(httpx2.AsyncBaseTransport):
    """The mock provider, keeping the body of every request the gateway sends it, so a test can
    prove what a model was (and was not) shown."""

    def __init__(self, log: list[str]) -> None:
        self._inner = httpx2.ASGITransport(app=mock_provider)
        self._log = log

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._log.append((await request.aread()).decode("utf-8", "replace"))
        return await self._inner.handle_async_request(request)


@asynccontextmanager
async def app_client(
    settings: Settings,
    *,
    client_ip: str = "127.0.0.1",
    span_exporter: SpanExporter | None = None,
    provider_log: list[str] | None = None,
) -> AsyncIterator[tuple[FastAPI, httpx2.AsyncClient]]:
    providers = httpx2.AsyncClient(
        mounts={
            "http://mock-llm:8000": (
                _Recording(provider_log)
                if provider_log is not None
                else httpx2.ASGITransport(app=mock_provider)
            ),
            "http://docparse:8000": httpx2.ASGITransport(app=fake_docparse.app),
            "https://idp.test": httpx2.ASGITransport(app=fake_idp.app),
        }
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
