"""Integration fixtures: one migrated PostgreSQL 19 database per test session."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest

from gateway.adapters.postgres.database import Database
from tests.support.database import DisposableDatabase, create_test_database, drop_test_database


@pytest.fixture(scope="session")
def test_db() -> Iterator[DisposableDatabase]:
    server_url = os.environ.get("TEST_DATABASE_URL")
    if not server_url:
        pytest.skip("TEST_DATABASE_URL not set; integration tests need PostgreSQL 19")
    db = create_test_database(server_url)
    try:
        yield db
    finally:
        drop_test_database(server_url, db.name)


@pytest.fixture(scope="session")
async def app_db(test_db: DisposableDatabase) -> AsyncIterator[Database]:
    db = Database(test_db.app_url, pool_size=10, max_overflow=10)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture(scope="session")
async def worker_db(test_db: DisposableDatabase) -> AsyncIterator[Database]:
    db = Database(test_db.worker_url, pool_size=2, max_overflow=2)
    try:
        yield db
    finally:
        await db.dispose()
