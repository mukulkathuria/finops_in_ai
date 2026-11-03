"""US-02: teams, team membership and single-use expiring invitations.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE teams (
      id         uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id     uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      name       text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, id)
    );
    CREATE UNIQUE INDEX teams_org_name_key ON teams (org_id, lower(name));
    ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
    ALTER TABLE teams FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON teams
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON teams FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT, UPDATE, DELETE ON teams TO app;
    GRANT SELECT ON teams TO worker, readonly_analytics;

    CREATE TABLE team_members (
      org_id     uuid NOT NULL,
      team_id    uuid NOT NULL,
      user_id    uuid NOT NULL,
      added_by   uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (team_id, user_id),
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE,
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE
    );
    CREATE INDEX team_members_user ON team_members (user_id);
    ALTER TABLE team_members ENABLE ROW LEVEL SECURITY;
    ALTER TABLE team_members FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON team_members
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    CREATE POLICY worker_read ON team_members FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT, DELETE ON team_members TO app;
    GRANT SELECT ON team_members TO worker, readonly_analytics;

    -- Team-scoped grants must point at a team of the same org.
    ALTER TABLE role_assignments ADD CONSTRAINT role_assignments_team_fk
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE;

    CREATE TABLE invitations (
      id         uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id     uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      email      text NOT NULL CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 320),
      org_role   text NOT NULL REFERENCES roles (name) CHECK (org_role <> 'team_lead'),
      team_id    uuid,
      team_role  text CHECK (team_role IN ('member', 'lead')),
      token_hash text NOT NULL UNIQUE CHECK (char_length(token_hash) = 64),
      invited_by uuid NOT NULL,
      expires_at timestamptz NOT NULL,
      used_at    timestamptz,
      revoked_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, team_id) REFERENCES teams (org_id, id) ON DELETE CASCADE,
      CHECK ((team_id IS NULL) = (team_role IS NULL))
    );
    -- At most one usable invitation per email and org; re-inviting supersedes the old one.
    CREATE UNIQUE INDEX invitations_one_pending ON invitations (org_id, email)
      WHERE used_at IS NULL AND revoked_at IS NULL;
    ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE invitations FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON invitations
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    -- Pre-tenant acceptance: only the invitation whose secret token the caller holds.
    CREATE POLICY accept_lookup ON invitations FOR SELECT USING (token_hash = app_lookup('token_hash'));
    GRANT SELECT, INSERT, UPDATE ON invitations TO app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE invitations;
    ALTER TABLE role_assignments DROP CONSTRAINT role_assignments_team_fk;
    DROP TABLE team_members;
    DROP TABLE teams;
    """)
