"""Disposable PostgreSQL 19 databases for integration tests.

Each test session creates `finops_test_<pid>_<rand>`, applies the same database setup SQL as the
container bootstrap, migrates it as `migrator`, and drops it at the end. Tests connect through the
real login roles (app, worker, readonly_analytics), so RLS is exercised exactly as in production.

Needs TEST_DATABASE_URL (a superuser URL to the server) and the FINOPS_*_PASSWORD role passwords.
"""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
SETUP_SQL = ROOT / "infra" / "docker" / "postgres" / "sql" / "database-setup.sql"
ROLE_PASSWORD_ENV = {
    "migrator": "FINOPS_MIGRATOR_PASSWORD",
    "app": "FINOPS_APP_PASSWORD",
    "worker": "FINOPS_WORKER_PASSWORD",
    "readonly_analytics": "FINOPS_READONLY_PASSWORD",
}


@dataclass(frozen=True, slots=True)
class DisposableDatabase:
    name: str
    superuser_url: str
    migrator_url: str
    app_url: str
    worker_url: str
    readonly_url: str


def _with(url: str, *, user: str | None = None, password: str | None = None, db: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    user = user or parts.username or ""
    password = password if password is not None else (parts.password or "")
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, f"/{db}", "", ""))


def _role_url(server_url: str, role: str, db: str) -> str:
    password = os.environ.get(ROLE_PASSWORD_ENV[role])
    if not password:
        raise RuntimeError(f"{ROLE_PASSWORD_ENV[role]} must be set for integration tests")
    return _with(server_url, user=role, password=password, db=db)


def create_test_database(server_url: str) -> DisposableDatabase:
    name = f"finops_test_{os.getpid()}_{secrets.token_hex(3)}"
    ident = sql.Identifier(name)
    with psycopg.connect(server_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER migrator").format(ident))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident))
        conn.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO migrator, app, worker, readonly_analytics"
            ).format(ident)
        )
    superuser_url = _with(server_url, db=name)
    with psycopg.connect(superuser_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(SETUP_SQL.read_text(encoding="utf-8"))  # type: ignore[call-overload]
    migrator_url = _role_url(server_url, "migrator", name)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", migrator_url.replace("%", "%%"))
    cfg.attributes["skip_logging_config"] = True  # keep pytest's logging intact
    command.upgrade(cfg, "head")
    return DisposableDatabase(
        name=name,
        superuser_url=superuser_url,
        migrator_url=migrator_url,
        app_url=_role_url(server_url, "app", name),
        worker_url=_role_url(server_url, "worker", name),
        readonly_url=_role_url(server_url, "readonly_analytics", name),
    )


def drop_test_database(server_url: str, name: str) -> None:
    with psycopg.connect(server_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
