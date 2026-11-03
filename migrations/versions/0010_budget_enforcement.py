"""US-07, US-08: budget period functions, one-time grants and budget-request targets.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Period boundaries in the org timezone (spec section 6). One definition shared by the request
    -- path, the usage views and the janitor, so budget counters and the ledger always agree.
    CREATE FUNCTION budget_period_start(p_period text, p_at timestamptz, p_tz text) RETURNS date
      LANGUAGE sql STABLE PARALLEL SAFE
      RETURN CASE p_period
        WHEN 'day' THEN (p_at AT TIME ZONE p_tz)::date
        WHEN 'week' THEN date_trunc('week', p_at AT TIME ZONE p_tz)::date
        ELSE date_trunc('month', p_at AT TIME ZONE p_tz)::date
      END;
    CREATE FUNCTION budget_period_end(p_period text, p_start date) RETURNS date
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      RETURN (p_start + CASE p_period WHEN 'day' THEN interval '1 day'
                                      WHEN 'week' THEN interval '7 days'
                                      ELSE interval '1 month' END)::date;
    REVOKE ALL ON FUNCTION budget_period_start(text, timestamptz, text),
                           budget_period_end(text, date) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION budget_period_start(text, timestamptz, text),
                              budget_period_end(text, date) TO app, worker, readonly_analytics;

    -- A one-time approval raises the limit for one period only (it expires at period end).
    ALTER TABLE budget_usage ADD COLUMN granted bigint NOT NULL DEFAULT 0
      CHECK (granted BETWEEN 0 AND 1000000000000000);

    -- A request for more budget names the budget it wants raised.
    ALTER TABLE budget_requests
      ADD COLUMN budget_id uuid NOT NULL,
      ADD COLUMN decision_note text CHECK (char_length(decision_note) <= 1000),
      ADD FOREIGN KEY (org_id, budget_id) REFERENCES budgets (org_id, id) ON DELETE CASCADE;
    CREATE INDEX budget_requests_pending ON budget_requests (org_id, team_id) WHERE status = 'pending';
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX budget_requests_pending;
    ALTER TABLE budget_requests DROP COLUMN decision_note, DROP COLUMN budget_id;
    ALTER TABLE budget_usage DROP COLUMN granted;
    DROP FUNCTION budget_period_end(text, date);
    DROP FUNCTION budget_period_start(text, timestamptz, text);
    """)
