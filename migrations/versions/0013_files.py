"""US-14: uploaded files. The original lives in the object store under a key made from the file
id (never from its name); the row records what validation established about it.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE files (
      id          uuid PRIMARY KEY,
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      team_id     uuid REFERENCES teams (id) ON DELETE SET NULL,
      user_id     uuid REFERENCES users (id) ON DELETE SET NULL,
      api_key_id  uuid REFERENCES api_keys (id) ON DELETE SET NULL,
      filename    text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
      kind        text NOT NULL CHECK (kind IN ('pdf', 'png', 'jpeg', 'gif', 'webp', 'docx',
                                                'xlsx', 'pptx', 'text')),
      mime        text NOT NULL CHECK (char_length(mime) BETWEEN 1 AND 127),
      size        bigint NOT NULL CHECK (size > 0),
      sha256      bytea NOT NULL CHECK (octet_length(sha256) = 32),
      pages       integer CHECK (pages >= 0),
      purpose     text NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 32),
      storage_key text NOT NULL UNIQUE CHECK (char_length(storage_key) BETWEEN 1 AND 255),
      created_at  timestamptz NOT NULL DEFAULT now()
    );
    -- Visibility is "the caller's team, or the caller": both are index scans, newest first.
    CREATE INDEX files_team ON files (org_id, team_id, id DESC);
    CREATE INDEX files_user ON files (org_id, user_id, id DESC);
    ALTER TABLE files ENABLE ROW LEVEL SECURITY;
    ALTER TABLE files FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON files
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON files FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, DELETE ON files TO app;
    GRANT SELECT, DELETE ON files TO worker;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE files;")
