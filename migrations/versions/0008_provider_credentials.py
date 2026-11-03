"""US-06: per-org provider keys, envelope-encrypted, never returned.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE provider_credentials (
      id          uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      provider    text NOT NULL CHECK (provider ~ '^[a-z0-9_-]{1,40}$'),
      label       text NOT NULL DEFAULT '' CHECK (char_length(label) <= 100),
      ciphertext  bytea NOT NULL,
      wrapped_key bytea NOT NULL,
      key_id      text NOT NULL,
      key_hint    text NOT NULL CHECK (char_length(key_hint) <= 8),
      created_by  uuid NOT NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      revoked_at  timestamptz
    );
    -- One active key per provider per org; adding a new one revokes the previous (rotation).
    CREATE UNIQUE INDEX provider_credentials_active ON provider_credentials (org_id, provider)
      WHERE revoked_at IS NULL;
    ALTER TABLE provider_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE provider_credentials FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON provider_credentials
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    GRANT SELECT, INSERT, UPDATE ON provider_credentials TO app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE provider_credentials;")
