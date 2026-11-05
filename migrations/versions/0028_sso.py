"""US-27: single sign-on through each org's OpenID Connect provider (spec sections 5 and 7).

- `org_sso`: an org's provider. It holds:
  - the issuer and client id;
  - the client secret, sealed with the org envelope as provider keys are (none for a public
    client, which relies on PKCE alone);
  - the email domains a first sign-in may link;
  - whether local login is off for the org's members (admins keep it, to recover from a broken
    provider).
- `identities`: an identity at a provider, `(org, issuer, subject)`, mapped to an existing user.
  A first sign-in links it through an email the provider has verified and the org already has.
- `sso_logins`: short-lived single-use secrets, stored only as hashes. Each is either the state
  of a sign-in in progress (with its nonce and PKCE verifier) or the one-time code the web app
  exchanges for a session.

As with invitations and refresh tokens, these are found before any tenant context only by the
hash of a secret the caller holds. To start a sign-in, an org is found by its slug.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

TABLES = ("org_sso", "identities", "sso_logins")


def upgrade() -> None:
    op.execute("""
    CREATE TABLE org_sso (
      org_id               uuid PRIMARY KEY REFERENCES orgs (id) ON DELETE CASCADE,
      issuer               text NOT NULL CHECK (char_length(issuer) BETWEEN 8 AND 500),
      client_id            text NOT NULL CHECK (char_length(client_id) BETWEEN 1 AND 500),
      secret_ciphertext    bytea,
      secret_wrapped_key   bytea,
      secret_key_id        text,
      allowed_domains      text[] NOT NULL DEFAULT '{}' CHECK (cardinality(allowed_domains) <= 50),
      local_login_disabled boolean NOT NULL DEFAULT false,
      updated_by           uuid REFERENCES users (id) ON DELETE SET NULL,
      updated_at           timestamptz NOT NULL DEFAULT now(),
      CHECK (num_nulls(secret_ciphertext, secret_wrapped_key, secret_key_id) IN (0, 3))
    );

    CREATE TABLE identities (
      id            uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
      provider      text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 500),
      subject       text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
      email         text CHECK (char_length(email) <= 320),
      created_at    timestamptz NOT NULL DEFAULT now(),
      last_login_at timestamptz,
      UNIQUE (org_id, provider, subject)
    );
    CREATE INDEX identities_user ON identities (user_id) INCLUDE (org_id);

    CREATE TABLE sso_logins (
      secret_hash   text PRIMARY KEY CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
      org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      kind          text NOT NULL CHECK (kind IN ('state', 'code')),
      nonce         text CHECK (char_length(nonce) <= 200),
      code_verifier text CHECK (char_length(code_verifier) <= 200),
      user_id       uuid REFERENCES users (id) ON DELETE CASCADE,
      expires_at    timestamptz NOT NULL,
      created_at    timestamptz NOT NULL DEFAULT now(),
      CHECK ((kind = 'state' AND nonce IS NOT NULL AND code_verifier IS NOT NULL
              AND user_id IS NULL)
          OR (kind = 'code' AND user_id IS NOT NULL AND nonce IS NULL
              AND code_verifier IS NULL))
    );
    CREATE INDEX sso_logins_expiry ON sso_logins (org_id, expires_at);
    """)
    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
        """)
    op.execute("""
    CREATE POLICY secret_lookup ON sso_logins FOR SELECT
      USING (secret_hash = app_lookup('sso_secret'));
    CREATE POLICY slug_lookup ON orgs FOR SELECT USING (slug = app_lookup('org_slug'));
    GRANT SELECT, INSERT, UPDATE, DELETE ON org_sso TO app;
    GRANT SELECT, INSERT, DELETE ON identities, sso_logins TO app;
    GRANT UPDATE (last_login_at, email) ON identities TO app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY slug_lookup ON orgs;
    DROP TABLE sso_logins, identities, org_sso;
    """)
