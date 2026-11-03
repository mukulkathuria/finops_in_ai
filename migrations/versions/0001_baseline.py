"""Phase 0 schema v0: tenant helpers, orgs, and the RLS pattern every tenant table repeats.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Tenant context (ADR 0002). Empty or missing settings map to NULL, so no context = no rows.
    CREATE FUNCTION app_current_org() RETURNS uuid
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$;
    CREATE FUNCTION app_current_user() RETURNS uuid
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
    -- Pre-tenant lookups: exposes only rows whose secret hash or email the caller already holds.
    CREATE FUNCTION app_lookup(name text) RETURNS text
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT NULLIF(current_setting('app.lookup_' || name, true), '') $$;
    CREATE FUNCTION app_current_groups() RETURNS text[]
      LANGUAGE sql STABLE PARALLEL SAFE
      AS $$ SELECT string_to_array(NULLIF(current_setting('app.groups', true), ''), ',') $$;
    CREATE FUNCTION set_updated_at() RETURNS trigger
      LANGUAGE plpgsql
      AS $$ BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

    REVOKE ALL ON FUNCTION app_current_org(), app_current_user(), app_lookup(text),
                           app_current_groups(), set_updated_at() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app_current_org(), app_current_user(), app_lookup(text),
                              app_current_groups() TO app, worker, readonly_analytics;

    CREATE TABLE orgs (
      id          uuid PRIMARY KEY DEFAULT uuidv7(),
      name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
      slug        text NOT NULL CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'),
      timezone    text NOT NULL DEFAULT 'UTC' CHECK (char_length(timezone) <= 64),
      settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at  timestamptz NOT NULL DEFAULT now(),
      updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX orgs_slug_key ON orgs (slug);
    CREATE TRIGGER orgs_updated_at BEFORE UPDATE ON orgs
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- The RLS pattern (repeated on every tenant table; enforced by scripts/audit_rls.py).
    ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON orgs
      USING (id = app_current_org()) WITH CHECK (id = app_current_org());
    CREATE POLICY worker_read ON orgs FOR SELECT TO worker USING (true);

    GRANT SELECT, INSERT, UPDATE ON orgs TO app;
    GRANT SELECT ON orgs TO worker, readonly_analytics;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE orgs;
    DROP FUNCTION set_updated_at(), app_current_groups(), app_lookup(text),
                  app_current_user(), app_current_org();
    """)
