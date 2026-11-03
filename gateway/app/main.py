"""Composition root: the only module that mounts the data plane and the control plane (ADR 0001).

Run with `uvicorn gateway.app.main:create_app --factory`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway import __version__
from gateway.api import health
from gateway.app.container import build_container
from gateway.app.logging import configure_logging
from gateway.app.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()  # values come from the environment
    configure_logging(settings.log_level, settings.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(settings)
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()

    docs = not settings.is_production_like
    app = FastAPI(
        title="AI FinOps Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(health.router)
    return app
