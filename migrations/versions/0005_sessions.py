"""US-03: rotating refresh tokens with family revocation; shared rate-limit counters.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE refresh_tokens (
      id                uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id            uuid NOT NULL,
      user_id           uuid NOT NULL,
      family_id         uuid NOT NULL,
      token_hash        text NOT NULL UNIQUE CHECK (char_length(token_hash) = 64),
      issued_at         timestamptz NOT NULL DEFAULT now(),
      expires_at        timestamptz NOT NULL,
      family_expires_at timestamptz NOT NULL,
      rotated_at        timestamptz,
      revoked_at        timestamptz,
      revoked_reason    text CHECK (revoked_reason IN ('reuse', 'logout', 'admin', 'password_change')),
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE
    );
    CREATE INDEX refresh_tokens_family ON refresh_tokens (family_id);
    CREATE INDEX refresh_tokens_user ON refresh_tokens (user_id);
    ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;
    ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON refresh_tokens
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    -- Pre-tenant refresh: only the row whose secret the caller presents.
    CREATE POLICY refresh_lookup ON refresh_tokens FOR SELECT USING (token_hash = app_lookup('token_hash'));
    CREATE POLICY worker_cleanup ON refresh_tokens FOR ALL TO worker USING (true);
    GRANT SELECT, INSERT, UPDATE ON refresh_tokens TO app;
    GRANT SELECT, DELETE ON refresh_tokens TO worker;

    -- Fixed-window counters for edge rate limits (keys are hashed client identifiers, no tenant
    -- data). UNLOGGED: losing counters on a crash only resets windows, so skip WAL overhead.
    CREATE UNLOGGED TABLE rate_limit_counters (
      key          text NOT NULL,
      window_start timestamptz NOT NULL,
      hits         integer NOT NULL,
      PRIMARY KEY (key, window_start)
    );
    GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_counters TO app, worker;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE rate_limit_counters;
    DROP TABLE refresh_tokens;
    """)
