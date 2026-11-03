"""Composition of long-lived services (constructor injection, no globals)."""

from dataclasses import dataclass

from gateway.adapters.postgres.database import Database
from gateway.app.settings import Settings


@dataclass(slots=True)
class Container:
    settings: Settings
    db: Database

    async def aclose(self) -> None:
        await self.db.dispose()


def build_container(settings: Settings) -> Container:
    db = Database(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        application_name=settings.service_name,
    )
    return Container(settings=settings, db=db)
