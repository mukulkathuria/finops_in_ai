"""Transactional outbox and org webhooks: budget alerts leave the gateway (spec sections 4, 6).

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- One alert endpoint per org. The URL can itself be a credential (chat webhooks carry tokens
    -- in the path), so URL and signing secret are sealed together; url_hint is for display only.
    CREATE TABLE webhook_endpoints (
      org_id      uuid PRIMARY KEY REFERENCES orgs (id) ON DELETE CASCADE,
      url_hint    text NOT NULL CHECK (char_length(url_hint) <= 300),
      ciphertext  bytea NOT NULL,
      wrapped_key bytea NOT NULL,
      key_id      text NOT NULL,
      created_by  uuid NOT NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE TRIGGER webhook_endpoints_updated_at BEFORE UPDATE ON webhook_endpoints
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Written in the same transaction as the event it announces; the worker delivers it
    -- at least once (receivers dedupe on the outbox id).
    CREATE TABLE outbox (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      topic        text NOT NULL CHECK (topic ~ '^[a-z_]+([.][a-z_]+)+$'),
      payload      jsonb NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      available_at timestamptz NOT NULL DEFAULT now(),
      attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
      last_error   text CHECK (char_length(last_error) <= 500),
      delivered_at timestamptz,
      failed_at    timestamptz
    );
    CREATE INDEX outbox_due ON outbox (available_at)
      WHERE delivered_at IS NULL AND failed_at IS NULL;
    CREATE INDEX outbox_org ON outbox (org_id, created_at DESC);

    DO $rls$
    DECLARE t text;
    BEGIN
      FOREACH t IN ARRAY ARRAY['webhook_endpoints', 'outbox'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (org_id = app_current_org())'
                       ' WITH CHECK (org_id = app_current_org())', t);
      END LOOP;
    END $rls$;
    CREATE POLICY worker_read ON webhook_endpoints FOR SELECT TO worker USING (true);
    CREATE POLICY worker_all ON outbox FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO app;
    GRANT SELECT ON webhook_endpoints TO worker;
    GRANT SELECT, INSERT ON outbox TO app;
    GRANT SELECT, UPDATE, DELETE ON outbox TO worker;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE outbox;
    DROP TABLE webhook_endpoints;
    """)
