"""Migrations that move rows carry every org's rows over, both ways. Deployments migrate as
`migrator`, which forced row-level security binds like any other role: a copy that reads a
tenant table without lifting it sees nothing (Phase 5 review, S-A1)."""

import os
from collections.abc import Iterator, Mapping

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


@pytest.fixture
def at_0025() -> Iterator[DisposableDatabase]:
    server_url = os.environ.get("TEST_DATABASE_URL")
    if not server_url:
        pytest.skip("TEST_DATABASE_URL not set; integration tests need PostgreSQL 19")
    db = create_test_database(server_url, revision="0025")
    try:
        yield db
    finally:
        drop_test_database(server_url, db.name)


def _seed(db: DisposableDatabase, chunks: Mapping[str, int]) -> None:
    """Each org gets one document with `n` chunks, written as the superuser."""
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        for slug, n in chunks.items():
            org = conn.execute(_ORG, {"slug": slug}).fetchone()
            assert org is not None
            doc = conn.execute(_DOCUMENT, {"org": org[0], "n": n}).fetchone()
            assert doc is not None
            conn.execute(_CHUNKS, {"org": org[0], "doc": doc[0], "n": n})


def _state(db: DisposableDatabase) -> tuple[dict[str, int], tuple[bool, bool]]:
    with psycopg.connect(db.superuser_url, autocommit=True) as conn:
        counts = {str(slug): int(n) for slug, n in conn.execute(_COUNTS).fetchall()}
        security = conn.execute(_SECURITY).fetchone()
    assert security is not None
    return counts, (bool(security[0]), bool(security[1]))


def test_0026_moves_every_orgs_chunks_into_the_partitions_and_back(
    at_0025: DisposableDatabase,
) -> None:
    seeded = {"org-a": 3, "org-b": 2}
    _seed(at_0025, seeded)

    migrate_up(at_0025, "0026")
    assert _state(at_0025) == (seeded, (True, True))
    with psycopg.connect(at_0025.superuser_url, autocommit=True) as conn:
        partitions = conn.execute(_OPEN_PARTITIONS).fetchone()
    assert partitions == (0, 16)  # reachable only through `chunks`, under its policies

    migrate_down(at_0025, "0025")
    assert _state(at_0025) == (seeded, (True, True))

    migrate_up(at_0025)  # and on to head
    assert _state(at_0025) == (seeded, (True, True))
