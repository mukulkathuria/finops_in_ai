"""US-18: cached answers for the exact and semantic response cache.

Entries are scoped by org (row-level security) and keyed by a hash that covers the caller's
permission scope; the semantic lookup searches by embedding inside one partition (scope, model,
parameters, system prompt and policy).

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE cache_entries (
      id              uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      key_hash        bytea NOT NULL CHECK (octet_length(key_hash) = 32),
      partition_hash  bytea NOT NULL CHECK (octet_length(partition_hash) = 32),
      response        jsonb NOT NULL,
      cost_micro_usd  bigint NOT NULL CHECK (cost_micro_usd >= 0),
      embedding       vector(1024),
      embedding_model text,
      expires_at      timestamptz NOT NULL,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, key_hash)
    );
    CREATE INDEX cache_entries_partition ON cache_entries (org_id, partition_hash);
    CREATE INDEX cache_entries_expiry ON cache_entries (org_id, expires_at);
    CREATE INDEX cache_entries_embedding ON cache_entries USING hnsw (embedding vector_cosine_ops);
    ALTER TABLE cache_entries ENABLE ROW LEVEL SECURITY;
    ALTER TABLE cache_entries FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON cache_entries
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON cache_entries FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, UPDATE, DELETE ON cache_entries TO app;
    GRANT SELECT, DELETE ON cache_entries TO worker;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE cache_entries;")
