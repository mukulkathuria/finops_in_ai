"""Phase 4 latency gate (spec section 11): the filtered vector search at 1M chunks.

The first seeded run measured a p95 of 482 ms against the 50 ms target. One HNSW graph of 1M
vectors (7.8 GB) served every org, so a search read pages across all of it and passed over about
260 rows of other orgs for the five it returned. Two changes, measured again by the `bench`
workflow:

- `chunks` is hash-partitioned by `org_id` (16 partitions). A search prunes to its org's
  partition and walks that partition's much smaller graph, where most rows are the org's own.
- The HNSW index is built on `embedding::halfvec(1024)`: half the size, the same ranking to about
  three significant digits. The column keeps its full precision.

Keys carry `org_id`, as a partitioned table's keys must. Partitions get no grants: everything
goes through `chunks`, under its row-level security.

Moving the rows, both ways (Phase 5 review, S-A1):

- The copy runs as the table's owner with FORCE ROW LEVEL SECURITY lifted. Forced, the tenant
  policy hides every row from a migration session (it has no org), so the copy would move
  nothing before the old table is dropped. The counts are compared before the drop.
- The new table has its foreign keys before the rows arrive: each row is then checked by the
  referential checks, which see past row-level security. Added to a filled table, the bulk
  check would read `orgs` and `documents` under their forced tenant policies, find no org, and
  refuse every row.
- `documents` (its writes) and then `chunks` (everything) are locked first, in the order an
  ingest takes them: no chunk commits between the copy and the drop. Ingestion and search wait
  for the migration.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

PARTITIONS = 16

_LOCK = """
    LOCK TABLE documents IN SHARE ROW EXCLUSIVE MODE;
    LOCK TABLE chunks IN ACCESS EXCLUSIVE MODE;
    ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY;
"""

_SECURITY = """
    ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON chunks
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON chunks FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, DELETE ON chunks TO app;
    GRANT SELECT, DELETE ON chunks TO worker;
    ANALYZE chunks;
"""


def _references(table: str) -> str:
    return f"""
    ALTER TABLE {table} ADD CONSTRAINT chunks_org_id_fkey
      FOREIGN KEY (org_id) REFERENCES orgs (id) ON DELETE CASCADE;
    ALTER TABLE {table} ADD CONSTRAINT chunks_document_id_fkey
      FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE;
    """


def _move_to(target: str) -> str:
    """Copy every chunk into `target`, refuse to go on unless all of them arrived, and put
    `target` in the old table's place."""
    return f"""
    INSERT INTO {target} SELECT * FROM chunks;
    DO $$
    DECLARE
      copied bigint;
      total bigint;
    BEGIN
      SELECT count(*) INTO copied FROM {target};
      SELECT count(*) INTO total FROM chunks;
      IF copied <> total THEN
        RAISE EXCEPTION 'chunks were lost in the copy: % of % rows arrived', copied, total;
      END IF;
    END $$;
    DROP TABLE chunks;
    ALTER TABLE {target} RENAME TO chunks;
    """


def upgrade() -> None:
    op.execute(_LOCK)
    op.execute("""
    CREATE TABLE chunks_by_org (LIKE chunks INCLUDING DEFAULTS INCLUDING CONSTRAINTS)
      PARTITION BY HASH (org_id);
    """)
    for i in range(PARTITIONS):
        op.execute(
            f"CREATE TABLE chunks_p{i} PARTITION OF chunks_by_org"
            f" FOR VALUES WITH (MODULUS {PARTITIONS}, REMAINDER {i})"
        )
    op.execute(
        _references("chunks_by_org")
        + _move_to("chunks_by_org")
        + """
    ALTER TABLE chunks ADD PRIMARY KEY (id, org_id);
    ALTER TABLE chunks ADD CONSTRAINT chunks_document_ordinal UNIQUE (document_id, ordinal, org_id);
    CREATE INDEX chunks_embedding ON chunks
      USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops);
    CREATE INDEX chunks_acl ON chunks USING gin (acl_groups);
    CREATE INDEX chunks_org ON chunks (org_id, embedding_model);
    """
        + _SECURITY
    )


def downgrade() -> None:
    op.execute(_LOCK)
    op.execute(
        "CREATE TABLE chunks_plain (LIKE chunks INCLUDING DEFAULTS INCLUDING CONSTRAINTS);"
        + _references("chunks_plain")
        + _move_to("chunks_plain")
        + """
    ALTER TABLE chunks ADD PRIMARY KEY (id);
    ALTER TABLE chunks ADD CONSTRAINT chunks_document_id_ordinal_key UNIQUE (document_id, ordinal);
    CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
    CREATE INDEX chunks_acl ON chunks USING gin (acl_groups);
    CREATE INDEX chunks_org ON chunks (org_id, embedding_model);
    """
        + _SECURITY
    )
