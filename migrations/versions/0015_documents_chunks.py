"""US-13: documents and chunks for retrieval under the caller's permissions.

Each chunk carries its document's groups so that one statement filters by permission and ranks
by vector distance (HNSW, cosine). The embedding model is stored per row; vectors of different
models are never compared.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE documents (
      id              uuid PRIMARY KEY,
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      title           text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
      file_id         uuid REFERENCES files (id) ON DELETE SET NULL,
      acl_groups      text[] NOT NULL CHECK (cardinality(acl_groups) BETWEEN 1 AND 200),
      created_by      uuid REFERENCES users (id) ON DELETE SET NULL,
      api_key_id      uuid REFERENCES api_keys (id) ON DELETE SET NULL,
      embedding_model text NOT NULL CHECK (char_length(embedding_model) BETWEEN 1 AND 200),
      chunk_count     integer NOT NULL CHECK (chunk_count > 0),
      token_count     integer NOT NULL CHECK (token_count >= 0),
      created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX documents_acl ON documents USING gin (acl_groups);

    CREATE TABLE chunks (
      id              uuid PRIMARY KEY,
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      document_id     uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
      ordinal         integer NOT NULL CHECK (ordinal >= 0),
      content         text NOT NULL CHECK (char_length(content) > 0),
      token_count     integer NOT NULL CHECK (token_count >= 0),
      acl_groups      text[] NOT NULL CHECK (cardinality(acl_groups) BETWEEN 1 AND 200),
      embedding_model text NOT NULL,
      embedding       vector(1024) NOT NULL,
      UNIQUE (document_id, ordinal)
    );
    CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
    CREATE INDEX chunks_acl ON chunks USING gin (acl_groups);

    ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE documents FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON documents
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON documents FOR ALL TO worker USING (true) WITH CHECK (true);
    ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON chunks
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON chunks FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, DELETE ON documents, chunks TO app;
    GRANT SELECT, DELETE ON documents, chunks TO worker;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE chunks; DROP TABLE documents;")
