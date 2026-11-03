"""US-04: team API keys (hash only, visible prefix, revocable).

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE api_keys (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL,
      team_id      uuid NOT NULL,
      user_id      uuid,
      name         text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
      prefix       text NOT NULL CHECK (prefix ~ '^[A-Za-z0-9]{8}$'),
      key_hash     text NOT NULL UNIQUE CHECK (char_length(key_hash) = 64),
      scopes       text[] NOT NULL DEFAULT '{models:use}',
      created_by   uuid NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      expires_at   timestamptz,
      last_used_at timestamptz,
      revoked_at   timestamptz,
      revoked_by   uuid,
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE,
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE,
      CHECK (scopes <@ ARRAY['models:use']::text[])
    );
    CREATE INDEX api_keys_team ON api_keys (team_id, created_at DESC);
    ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
    ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON api_keys
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    -- Pre-tenant authentication: only the key whose secret the caller presents.
    CREATE POLICY key_lookup ON api_keys FOR SELECT USING (key_hash = app_lookup('key_hash'));
    GRANT SELECT, INSERT, UPDATE ON api_keys TO app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE api_keys;")
