"""US-15, US-16: the parse cache. A derived artifact is keyed by content hash, backend name,
backend version and options, so two backends' results are never mixed; the backend that made it
is recorded with it.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE file_artifacts (
      id              uuid PRIMARY KEY,
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      sha256          bytea NOT NULL CHECK (octet_length(sha256) = 32),
      backend         text NOT NULL CHECK (char_length(backend) BETWEEN 1 AND 64),
      backend_version text NOT NULL CHECK (char_length(backend_version) BETWEEN 1 AND 300),
      options_hash    bytea NOT NULL CHECK (octet_length(options_hash) = 32),
      markdown        text NOT NULL,
      pages           jsonb NOT NULL CHECK (jsonb_typeof(pages) = 'array'),
      warnings        jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(warnings) = 'array'),
      tokens_before   integer NOT NULL CHECK (tokens_before >= 0),
      tokens_after    integer NOT NULL CHECK (tokens_after >= 0),
      gpu_seconds     double precision NOT NULL DEFAULT 0 CHECK (gpu_seconds >= 0),
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, sha256, backend, backend_version, options_hash)
    );
    ALTER TABLE file_artifacts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE file_artifacts FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON file_artifacts
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON file_artifacts FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT ON file_artifacts TO app;
    GRANT SELECT, DELETE ON file_artifacts TO worker;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE file_artifacts;")
