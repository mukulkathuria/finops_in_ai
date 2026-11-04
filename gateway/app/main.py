"""Composition root: the only module that mounts the data plane and the control plane (ADR 0001).

Run with `uvicorn gateway.app.main:create_app --factory`.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from control_plane.api import (
    api_keys,
    audit,
    auth,
    budgets,
    cache,
    guardrails,
    invitations,
    providers,
    savings,
    stages,
    teams,
    webhooks,
)
from control_plane.api.deps import ControlPlaneServices
from gateway import __version__
from gateway.api import documents, files, health, metrics, openai, usage, whoami
from gateway.api.caller import CallerServices
from gateway.api.errors import install_error_handlers
from gateway.api.metrics import MetricsServices
from gateway.api.openai import ChatServices
from gateway.api.usage import UsageServices
from gateway.app.container import Container, build_container
from gateway.app.logging import configure_logging
from gateway.app.middleware import (
    BodyLimitMiddleware,
    RequestContextMiddleware,
    TelemetryMiddleware,
)
from gateway.app.observability import build_observability
from gateway.app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx2.AsyncClient | None = None,
    environ: Mapping[str, str] | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    settings = settings or Settings()  # values come from the environment
    configure_logging(settings.log_level, settings.service_name)
    observability = build_observability(settings, span_exporter=span_exporter)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(
            settings, http_client=http_client, environ=environ, observability=observability
        )
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()
            await asyncio.to_thread(observability.shutdown)  # flush queued spans

    docs = not settings.is_production_like
    app = FastAPI(
        title="AI FinOps Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    install_error_handlers(app)
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        upload_bytes=settings.max_upload_bytes + 1024 * 1024,  # room for multipart framing
    )
    app.add_middleware(
        TelemetryMiddleware,
        metrics=observability.metrics,
        tracer=observability.telemetry.tracer,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(auth.well_known)
    app.include_router(auth.router)
    app.include_router(audit.router)
    app.include_router(teams.router)
    app.include_router(invitations.router)
    app.include_router(invitations.accept_router)
    app.include_router(api_keys.router)
    app.include_router(providers.router)
    app.include_router(budgets.router)
    app.include_router(budgets.requests_router)
    app.include_router(webhooks.router)
    app.include_router(guardrails.router)
    app.include_router(stages.router)
    app.include_router(savings.router)
    app.include_router(cache.router)
    app.include_router(files.router)
    app.include_router(documents.router)
    app.include_router(whoami.router)
    app.include_router(usage.router)
    app.include_router(openai.router)
    return app


def _container_satisfies_every_api_protocol(
    container: Container,
) -> tuple[ControlPlaneServices, CallerServices, ChatServices, UsageServices, MetricsServices]:
    """Type-level check only: mypy fails here if the container drifts from what routers need."""
    return container, container, container, container, container
