"""US-26: optimization stage reports and eval runs.

Stage reports are a high-volume table partitioned by month (pg_partman), written in the
settlement transaction next to the ledger row. An eval run's `passed` is computed by the database
from its score and threshold, so it cannot be recorded as passed when it did not.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE stage_reports (
      id                   uuid NOT NULL DEFAULT uuidv7(),
      org_id               uuid NOT NULL,
      request_id           text NOT NULL,
      ts                   timestamptz NOT NULL DEFAULT now(),
      stage                text NOT NULL CHECK (stage ~ '^[a-z][a-z0-9_]{0,63}$'),
      version              text NOT NULL CHECK (char_length(version) BETWEEN 1 AND 64),
      phase                text NOT NULL
                           CHECK (phase IN ('pre_auth', 'pre_llm', 'post_llm', 'async')),
      tokens_before        bigint NOT NULL CHECK (tokens_before >= 0),
      tokens_after         bigint NOT NULL CHECK (tokens_after >= 0),
      cost_delta_micro_usd bigint NOT NULL,
      latency_ms           integer NOT NULL CHECK (latency_ms >= 0),
      quality_risk         real NOT NULL CHECK (quality_risk BETWEEN 0 AND 1),
      applied              boolean NOT NULL,
      shadow               boolean NOT NULL,
      decisions            jsonb NOT NULL DEFAULT '{}'::jsonb,
      PRIMARY KEY (id, ts)
    ) PARTITION BY RANGE (ts);
    CREATE INDEX stage_reports_org_ts ON stage_reports (org_id, ts DESC);
    CREATE INDEX stage_reports_request ON stage_reports (org_id, request_id);
    SELECT partman.create_parent(
      p_parent_table := 'public.stage_reports', p_control := 'ts', p_interval := '1 month',
      p_premake := 4, p_jobmon := false);
    UPDATE partman.part_config SET infinite_time_partitions = true
     WHERE parent_table = 'public.stage_reports';
    ALTER TABLE stage_reports ENABLE ROW LEVEL SECURITY;
    ALTER TABLE stage_reports FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON stage_reports
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON stage_reports FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT ON stage_reports TO app;
    GRANT SELECT ON stage_reports TO worker, readonly_analytics;

    CREATE TABLE eval_runs (
      id          uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      stage       text NOT NULL CHECK (stage ~ '^[a-z][a-z0-9_]{0,63}$'),
      version     text NOT NULL CHECK (char_length(version) BETWEEN 1 AND 64),
      dataset     text NOT NULL CHECK (char_length(dataset) BETWEEN 1 AND 200),
      metric      text NOT NULL CHECK (char_length(metric) BETWEEN 1 AND 64),
      score       double precision NOT NULL CHECK (score BETWEEN -1e12 AND 1e12),
      threshold   double precision NOT NULL CHECK (threshold BETWEEN -1e12 AND 1e12),
      samples     integer NOT NULL CHECK (samples > 0),
      passed      boolean GENERATED ALWAYS AS (score >= threshold) STORED,
      created_by  uuid REFERENCES users (id) ON DELETE SET NULL,
      created_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX eval_runs_stage ON eval_runs (org_id, stage, version, created_at DESC);
    ALTER TABLE eval_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE eval_runs FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON eval_runs
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON eval_runs FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT ON eval_runs TO app;
    GRANT SELECT ON eval_runs TO worker, readonly_analytics;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE eval_runs;
    DELETE FROM partman.part_config WHERE parent_table = 'public.stage_reports';
    DROP TABLE stage_reports CASCADE;
    DROP TABLE IF EXISTS partman.template_public_stage_reports;
    """)
