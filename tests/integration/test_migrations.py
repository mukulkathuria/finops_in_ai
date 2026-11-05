"""Migrations that move or count rows carry every org's rows over, both ways. Deployments
migrate as `migrator`, which forced row-level security binds like any other role: a statement
that reads a tenant table without lifting it sees nothing (Phase 5 review, S-A1)."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import psycopg
import pytest

from tests.support.database import (
    DisposableDatabase,
    create_test_database,
    drop_test_database,
    migrate_down,
    migrate_up,
)

_ORG = "INSERT INTO orgs (name, slug) VALUES (%(slug)s, %(slug)s) RETURNING id"
_DOCUMENT = """
INSERT INTO documents (id, org_id, title, acl_groups, embedding_model, chunk_count, token_count)
VALUES (gen_random_uuid(), %(org)s, 'handbook', ARRAY['org'], 'm', %(n)s, %(n)s)
RETURNING id
"""
_CHUNKS = """
INSERT INTO chunks (id, org_id, document_id, ordinal, content, token_count, acl_groups,
                    embedding_model, embedding)
SELECT gen_random_uuid(), %(org)s, %(doc)s, g, 'chunk ' || g, 1, ARRAY['org'], 'm',
       array_fill(0.5::real, ARRAY[1024])::vector
FROM generate_series(0, %(n)s::int - 1) AS g
"""
_COUNTS = """
SELECT o.slug, count(*) FROM chunks c JOIN orgs o ON o.id = c.org_id GROUP BY o.slug
"""
_SECURITY = (
    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'chunks'::regclass"
)
_OPEN_PARTITIONS = """
SELECT count(*) FILTER (WHERE has_table_privilege('app', i.inhrelid, 'SELECT')
                           OR has_table_privilege('worker', i.inhrelid, 'SELECT')
                           OR has_table_privilege('readonly_analytics', i.inhrelid, 'SELECT')),
       count(*)
FROM pg_inherits i WHERE i.inhparent = 'chunks'::regclass
"""
_WORKSPACE = """
INSERT INTO workspaces (org_id, name, acl_groups) VALUES (%(org)s, 'research', ARRAY['org'])
RETURNING id
"""
_FACTS = """
INSERT INTO facts (org_id, workspace_id, content, agent, content_hash, token_count, embedding,
                   embedding_model)
SELECT %(org)s, %(workspace)s, 'fact ' || g, 'agent-a', sha256(('fact ' || g)::bytea), 2,
       array_fill(0.5::real, ARRAY[1024])::vector, 'm'
FROM generate_series(1, %(n)s::int) AS g
"""
_FACT_COUNTS = """
SELECT o.slug, w.fact_count FROM workspaces w JOIN orgs o ON o.id = w.org_id
"""


@pytest.fixture
def server_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; integration tests need PostgreSQL 19")
    return url


@contextmanager
def _database_at(server_url: str, revision: str) -> Iterator[DisposableDatabase]:
    db = create_test_database(server_url, revision=revision)
    try:
        yield db
    finally:
        drop_test_database(server_url, db.name)


@pytest.fixture
def at_0025(server_url: str) -> Iterator[DisposableDatabase]:
    with _database_at(server_url, "0025") as db:
        yield db


@pytest.fixture
def at_0026(server_url: str) -> Iterator[DisposableDatabase]:
    with _database_at(server_url, "0026") as db:
        yield db


def _seed_chunks(db: DisposableDatabase, chunks: Mapping[str, int]) -> None:
    """Each org gets one document with `n` chunks, written as the superuser."""
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        for slug, n in chunks.items():
            org = conn.execute(_ORG, {"slug": slug}).fetchone()
            assert org is not None
            doc = conn.execute(_DOCUMENT, {"org": org[0], "n": n}).fetchone()
            assert doc is not None
            conn.execute(_CHUNKS, {"org": org[0], "doc": doc[0], "n": n})


def _chunk_state(db: DisposableDatabase) -> tuple[dict[str, int], tuple[bool, bool]]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        counts = {str(slug): int(n) for slug, n in conn.execute(_COUNTS).fetchall()}
        security = conn.execute(_SECURITY).fetchone()
    assert security is not None
    return counts, (bool(security[0]), bool(security[1]))


def test_0026_moves_every_orgs_chunks_into_the_partitions_and_back(
    at_0025: DisposableDatabase,
) -> None:
    db, seeded = at_0025, {"org-a": 3, "org-b": 2}
    _seed_chunks(db, seeded)

    migrate_up(db, "0026")
    assert _chunk_state(db) == (seeded, (True, True))
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        partitions = conn.execute(_OPEN_PARTITIONS).fetchone()
    assert partitions == (0, 16)  # reachable only through `chunks`, under its policies

    migrate_down(db, "0025")
    assert _chunk_state(db) == (seeded, (True, True))

    migrate_up(db)  # and on to head
    assert _chunk_state(db) == (seeded, (True, True))


def test_0027_counts_the_facts_every_org_already_has(at_0026: DisposableDatabase) -> None:
    db = at_0026
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        for slug, n in (("org-a", 3), ("org-b", 1)):
            org = conn.execute(_ORG, {"slug": slug}).fetchone()
            assert org is not None
            workspace = conn.execute(_WORKSPACE, {"org": org[0]}).fetchone()
            assert workspace is not None
            conn.execute(_FACTS, {"org": org[0], "workspace": workspace[0], "n": n})

    migrate_up(db, "0027")
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        counted = {str(slug): int(n) for slug, n in conn.execute(_FACT_COUNTS).fetchall()}
        forced = conn.execute(
            "SELECT bool_and(relforcerowsecurity) FROM pg_class"
            " WHERE relname IN ('workspaces', 'facts')"
        ).fetchone()
    assert counted == {"org-a": 3, "org-b": 1}
    assert forced == (True,)  # lifted for the count only

    migrate_down(db, "0026")
    migrate_up(db)
