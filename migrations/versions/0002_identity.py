"""US-01: users, RBAC as data (roles, permissions, assignments) and the bootstrap flag.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ROLES = [
    ("org_admin", "org", "Everything in the org"),
    ("finance", "org", "Read-only reports, exports, price and baseline views"),
    ("auditor", "org", "Read-only audit log and configuration history"),
    ("member", "org", "Use allowed models within limits; own usage; request more budget"),
    ("team_lead", "team", "Manage members, limits, keys and requests inside one team"),
]
PERMISSIONS = [
    ("org:manage", "Org settings, policies and plugins"),
    ("users:manage", "Invite users, assign org roles, disable users"),
    ("teams:manage", "Create and delete teams, set team caps"),
    ("team_members:manage", "Add and remove members of a team"),
    ("providers:manage", "Provider credentials, adapters and models"),
    ("budgets:manage_org", "Org-level budgets"),
    ("budgets:manage_team", "Team budgets and caps"),
    ("budgets:manage_member", "Member limits inside a team"),
    ("budget_requests:decide", "Approve or deny requests for more budget"),
    ("budget_requests:create", "Ask for more budget"),
    ("api_keys:manage", "Create and revoke team API keys"),
    ("reports:read", "Org-wide usage, cost and savings reports and exports"),
    ("reports:read_team", "Usage, cost and savings reports for one team"),
    ("prices:read", "Price catalog and baseline views"),
    ("usage:read_self", "Own usage and remaining budget"),
    ("audit:read", "Audit log and verification"),
    ("config:read", "Configuration history"),
    ("models:use", "Call models through the gateway"),
]
GRANTS = {
    "org_admin": [p for p, _ in PERMISSIONS],
    "finance": ["reports:read", "reports:read_team", "prices:read", "usage:read_self"],
    "auditor": ["audit:read", "config:read", "usage:read_self"],
    "member": ["models:use", "usage:read_self", "budget_requests:create"],
    "team_lead": [
        "team_members:manage",
        "budgets:manage_member",
        "budget_requests:decide",
        "api_keys:manage",
        "reports:read_team",
    ],
}


def _values(rows: list[tuple[str, ...]]) -> str:
    return ",\n".join(
        "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in row) + ")" for row in rows
    )


def upgrade() -> None:
    op.execute(f"""
    -- RBAC reference data (global, read-only for runtime roles).
    CREATE TABLE roles (
      name        text PRIMARY KEY,
      scope       text NOT NULL CHECK (scope IN ('org', 'team')),
      description text NOT NULL
    );
    CREATE TABLE permissions (
      name        text PRIMARY KEY,
      description text NOT NULL
    );
    CREATE TABLE role_permissions (
      role       text NOT NULL REFERENCES roles (name) ON DELETE CASCADE,
      permission text NOT NULL REFERENCES permissions (name) ON DELETE CASCADE,
      PRIMARY KEY (role, permission)
    );
    INSERT INTO roles (name, scope, description) VALUES {_values(ROLES)};
    INSERT INTO permissions (name, description) VALUES {_values(PERMISSIONS)};
    INSERT INTO role_permissions (role, permission) VALUES
      {_values([(r, p) for r, ps in GRANTS.items() for p in ps])};
    GRANT SELECT ON roles, permissions, role_permissions TO app, worker, readonly_analytics;

    -- Global switches. bootstrap_completed flips once when the first org signs up.
    CREATE TABLE system_state (
      key        text PRIMARY KEY,
      value      jsonb NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO system_state (key, value) VALUES ('bootstrap_completed', 'false');
    GRANT SELECT, UPDATE (value, updated_at) ON system_state TO app;

    ALTER TABLE orgs ADD CONSTRAINT orgs_id_unique_for_fk UNIQUE (id);

    CREATE TABLE users (
      id              uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      email           text NOT NULL CHECK (email = lower(email) AND char_length(email) BETWEEN 3 AND 320),
      display_name    text NOT NULL DEFAULT '' CHECK (char_length(display_name) <= 200),
      password_hash   text CHECK (password_hash IS NULL OR password_hash LIKE '$argon2id$%'),
      status          text NOT NULL DEFAULT 'active' CHECK (status IN ('invited', 'active', 'disabled')),
      failed_logins   integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
      locked_until    timestamptz,
      last_login_at   timestamptz,
      created_at      timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, id)
    );
    -- One org per user in v1, so email is globally unique (login needs no org hint).
    CREATE UNIQUE INDEX users_email_key ON users (email);
    CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    ALTER TABLE users ENABLE ROW LEVEL SECURITY;
    ALTER TABLE users FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON users
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    -- Pre-tenant login lookup: only the row whose normalised email the caller supplied.
    CREATE POLICY login_lookup ON users FOR SELECT USING (email = app_lookup('email'));
    GRANT SELECT, INSERT, UPDATE ON users TO app;

    CREATE TABLE role_assignments (
      id         uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id     uuid NOT NULL,
      user_id    uuid NOT NULL,
      role       text NOT NULL REFERENCES roles (name),
      team_id    uuid,
      granted_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (org_id, user_id) REFERENCES users (org_id, id) ON DELETE CASCADE,
      CHECK ((role = 'team_lead') = (team_id IS NOT NULL))
    );
    CREATE UNIQUE INDEX role_assignments_unique ON role_assignments (user_id, role, team_id) NULLS NOT DISTINCT;
    CREATE INDEX role_assignments_org ON role_assignments (org_id);
    ALTER TABLE role_assignments ENABLE ROW LEVEL SECURITY;
    ALTER TABLE role_assignments FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON role_assignments
      USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
    GRANT SELECT, INSERT, DELETE ON role_assignments TO app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE role_assignments;
    DROP TABLE users;
    ALTER TABLE orgs DROP CONSTRAINT orgs_id_unique_for_fk;
    DROP TABLE system_state;
    DROP TABLE role_permissions;
    DROP TABLE permissions;
    DROP TABLE roles;
    """)
