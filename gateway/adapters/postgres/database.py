"""Async PostgreSQL access: one pooled engine, tenant-scoped transactions (ADR 0002).

Every request transaction sets `app.org_id`, `app.user_id` and `app.groups` with `set_config(...,
true)`, the parameterised form of `SET LOCAL`, so RLS policies see the caller and the values vanish
at commit or rollback. Raw SQL is always parameterised through `text()` bind parameters.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_SET_CONTEXT: Final = text(
    "SELECT set_config('app.org_id', :org_id, true),"
    " set_config('app.user_id', :user_id, true),"
    " set_config('app.groups', :groups, true)"
)
_SET_LOOKUP: Final = text("SELECT set_config('app.lookup_' || :name, :value, true)")
_LOOKUP_NAMES: Final = frozenset({"email", "key_hash", "token_hash"})
_READY_DEADLINE_S: Final = 2.0


def to_sqlalchemy_url(url: str, *, driver: str = "psycopg") -> str:
    """Accept libpq-style URLs (`postgresql://`) and select the psycopg 3 driver."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in {"postgres", "postgresql", f"postgresql+{driver}"}:
        raise ValueError("database URL must use the postgresql:// scheme")
    return f"postgresql+{driver}://{rest}"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who is acting. `groups` feeds ACL-filtered retrieval (RAG) in the same statement."""

    org_id: UUID
    user_id: UUID | None = None
    groups: tuple[str, ...] = ()

    def as_params(self) -> dict[str, str]:
        return {
            "org_id": str(self.org_id),
            "user_id": str(self.user_id) if self.user_id else "",
            "groups": ",".join(self.groups),
        }


class Database:
    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 20,
        max_overflow: int = 10,
        pool_timeout: float = 5.0,
        statement_timeout_ms: int = 5000,
        application_name: str = "finops-gateway",
    ) -> None:
        options = (
            f"-c statement_timeout={statement_timeout_ms}"
            f" -c idle_in_transaction_session_timeout={max(statement_timeout_ms * 2, 10_000)}"
            " -c lock_timeout=3000"
        )
        self._engine: AsyncEngine = create_async_engine(
            to_sqlalchemy_url(url),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=1800,
            connect_args={
                "application_name": application_name,
                "options": options,
                "connect_timeout": 10,  # never hang on an unreachable host
            },
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def transaction(self, ctx: TenantContext | None = None) -> AsyncIterator[AsyncConnection]:
        """Open a transaction; commit on success, roll back on any exception."""
        async with self._engine.begin() as conn:
            if ctx is not None:
                await conn.execute(_SET_CONTEXT, ctx.as_params())
            yield conn

    async def is_ready(self) -> bool:
        """True when a pooled connection answers `SELECT 1` within the readiness deadline."""
        try:
            async with asyncio.timeout(_READY_DEADLINE_S), self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except TimeoutError, OSError, SQLAlchemyError:
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()


async def set_tenant(conn: AsyncConnection, ctx: TenantContext) -> None:
    """Switch tenant context inside an open transaction (after a pre-tenant lookup)."""
    await conn.execute(_SET_CONTEXT, ctx.as_params())


async def set_lookup(conn: AsyncConnection, name: str, value: str) -> None:
    """Expose exactly one row through a hash-keyed lookup policy (ADR 0002, item 3)."""
    if name not in _LOOKUP_NAMES:
        raise ValueError(f"unknown lookup {name!r}")
    await conn.execute(_SET_LOOKUP, {"name": name, "value": value})


async def fetch_one(
    conn: AsyncConnection, sql: str, params: Mapping[str, Any] | None = None
) -> RowMapping | None:
    result = await conn.execute(text(sql), dict(params or {}))
    return result.mappings().first()


async def fetch_all(
    conn: AsyncConnection, sql: str, params: Mapping[str, Any] | None = None
) -> list[RowMapping]:
    result = await conn.execute(text(sql), dict(params or {}))
    return list(result.mappings().all())
