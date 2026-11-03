"""US-07..US-10: hierarchical budgets, period counters, reservations, alerts, budget requests.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE budgets (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      scope_type   text NOT NULL CHECK (scope_type IN ('org', 'team', 'member')),
      team_id      uuid,
      user_id      uuid,
      period       text NOT NULL CHECK (period IN ('day', 'week', 'month')),
      unit         text NOT NULL CHECK (unit IN ('usd', 'tokens', 'images')),
      limit_amount bigint NOT NULL CHECK (limit_amount BETWEEN 0 AND 1000000000000000),
      soft_pct     integer NOT NULL DEFAULT 80 CHECK (soft_pct BETWEEN 1 AND 100),
      created_by   uuid,
      created_at   timestamptz NOT NULL DEFAULT now(),
      updated_at   timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, id),
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE,
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE,
      CHECK ((scope_type = 'org' AND team_id IS NULL AND user_id IS NULL)
          OR (scope_type = 'team' AND team_id IS NOT NULL AND user_id IS NULL)
          OR (scope_type = 'member' AND team_id IS NOT NULL AND user_id IS NOT NULL))
    );
    CREATE UNIQUE INDEX budgets_scope_key ON budgets (org_id, scope_type, team_id, user_id, period, unit)
      NULLS NOT DISTINCT;
    CREATE TRIGGER budgets_updated_at BEFORE UPDATE ON budgets FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Counters per budget and period: a performance cache over the ledger (rebuildable).
    CREATE TABLE budget_usage (
      org_id       uuid NOT NULL,
      budget_id    uuid NOT NULL,
      period_start date NOT NULL,
      committed    bigint NOT NULL DEFAULT 0 CHECK (committed >= 0),
      reserved     bigint NOT NULL DEFAULT 0 CHECK (reserved >= 0),
      soft_alerted boolean NOT NULL DEFAULT false,
      PRIMARY KEY (budget_id, period_start),
      FOREIGN KEY (org_id, budget_id) REFERENCES budgets (org_id, id) ON DELETE CASCADE
    );

    CREATE TABLE budget_reservations (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL,
      request_id   text NOT NULL,
      budget_id    uuid NOT NULL,
      period_start date NOT NULL,
      amount       bigint NOT NULL CHECK (amount >= 0),
      status       text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'committed', 'released', 'expired')),
      expires_at   timestamptz NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, budget_id) REFERENCES budgets (org_id, id) ON DELETE CASCADE,
      UNIQUE (org_id, request_id, budget_id)
    );
    CREATE INDEX budget_reservations_open ON budget_reservations (expires_at) WHERE status = 'reserved';

    CREATE TABLE budget_alerts (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL,
      budget_id    uuid NOT NULL,
      period_start date NOT NULL,
      kind         text NOT NULL CHECK (kind IN ('soft_threshold')),
      committed    bigint NOT NULL,
      limit_amount bigint NOT NULL,
      created_at   timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, budget_id) REFERENCES budgets (org_id, id) ON DELETE CASCADE,
      UNIQUE (budget_id, period_start, kind)
    );

    CREATE TABLE budget_requests (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL,
      team_id      uuid NOT NULL,
      user_id      uuid NOT NULL,
      amount       bigint NOT NULL CHECK (amount > 0 AND amount <= 1000000000000000),
      unit         text NOT NULL CHECK (unit IN ('usd', 'tokens', 'images')),
      reason       text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 1000),
      status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied')),
      grant_type   text CHECK (grant_type IN ('one_time', 'permanent')),
      decided_by   uuid,
      decided_at   timestamptz,
      created_at   timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE,
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE
    );

    DO $rls$
    DECLARE t text;
    BEGIN
      FOREACH t IN ARRAY ARRAY['budgets', 'budget_usage', 'budget_reservations', 'budget_alerts', 'budget_requests'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (org_id = app_current_org())'
                       ' WITH CHECK (org_id = app_current_org())', t);
        EXECUTE format('CREATE POLICY worker_all ON %I FOR ALL TO worker USING (true) WITH CHECK (true)', t);
      END LOOP;
    END $rls$;
    GRANT SELECT, INSERT, UPDATE, DELETE ON budgets TO app;
    GRANT SELECT, INSERT, UPDATE ON budget_usage, budget_reservations, budget_requests TO app;
    GRANT SELECT, INSERT ON budget_alerts TO app;
    GRANT SELECT, INSERT, UPDATE ON budgets, budget_usage, budget_reservations, budget_alerts, budget_requests TO worker;
    GRANT SELECT ON budgets, budget_usage, budget_alerts TO readonly_analytics;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE budget_requests;
    DROP TABLE budget_alerts;
    DROP TABLE budget_reservations;
    DROP TABLE budget_usage;
    DROP TABLE budgets;
    """)
