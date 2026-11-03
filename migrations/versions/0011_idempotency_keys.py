"""US-10: Idempotency-Key storage (spec section 6: a retried request is never charged twice).

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Claimed in the transaction that reserves the budget, completed (with the canonical result)
    -- in the one that settles it; the janitor purges keys after a day.
    CREATE TABLE idempotency_keys (
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      caller      text NOT NULL CHECK (char_length(caller) BETWEEN 1 AND 80),
      key         text NOT NULL CHECK (char_length(key) BETWEEN 1 AND 255),
      fingerprint bytea NOT NULL CHECK (octet_length(fingerprint) = 32),
      status      text NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'completed')),
      response    bytea,
      created_at  timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (org_id, caller, key)
    );
    CREATE INDEX idempotency_keys_created ON idempotency_keys (created_at);
    ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
    ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON idempotency_keys
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_all ON idempotency_keys FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO app;
    GRANT SELECT, DELETE ON idempotency_keys TO worker;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_keys;")
