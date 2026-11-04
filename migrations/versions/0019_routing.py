"""US-19: judged samples of routed answers (eval_samples).

Each row records how far an answer from the small tier agreed with the large tier's answer to the
same request; the routing report compares their mean with the org's tolerance.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE eval_samples (
      id              uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      request_id      text NOT NULL,
      stage           text NOT NULL CHECK (stage ~ '^[a-z][a-z0-9_]{0,63}$'),
      candidate_model text NOT NULL CHECK (char_length(candidate_model) BETWEEN 1 AND 200),
      reference_model text NOT NULL CHECK (char_length(reference_model) BETWEEN 1 AND 200),
      score           double precision NOT NULL CHECK (score BETWEEN -1 AND 1),
      created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX eval_samples_stage ON eval_samples (org_id, stage, created_at DESC);
    ALTER TABLE eval_samples ENABLE ROW LEVEL SECURITY;
    ALTER TABLE eval_samples FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON eval_samples
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON eval_samples FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT ON eval_samples TO app;
    GRANT SELECT ON eval_samples TO worker, readonly_analytics;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE eval_samples;")
