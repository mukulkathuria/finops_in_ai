"""Phase 3 review: cached answers are sealed, and the worker only cleans the cache up.

- `cache_entries` holds sealed answers (envelope encryption bound to the org and the key); the
  plaintext column goes. Being a cache, its entries are dropped rather than migrated.
- The worker role can no longer read answers: it sees ids, orgs and times (to purge expired
  entries and keep each org under its cap) and may delete.
- Indexes for the janitor: expiry across orgs, and age within an org.

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    TRUNCATE cache_entries;
    ALTER TABLE cache_entries DROP COLUMN response;
    ALTER TABLE cache_entries
      ADD COLUMN ciphertext bytea NOT NULL,
      ADD COLUMN wrapped_key bytea NOT NULL,
      ADD COLUMN key_id text NOT NULL CHECK (char_length(key_id) BETWEEN 1 AND 64);
    DROP POLICY worker_all ON cache_entries;
    REVOKE SELECT, DELETE ON cache_entries FROM worker;
    GRANT SELECT (id, org_id, expires_at, created_at), DELETE ON cache_entries TO worker;
    CREATE POLICY worker_janitor_read ON cache_entries FOR SELECT TO worker USING (true);
    CREATE POLICY worker_janitor_delete ON cache_entries FOR DELETE TO worker USING (true);
    CREATE INDEX cache_entries_expires ON cache_entries (expires_at);
    CREATE INDEX cache_entries_age ON cache_entries (org_id, created_at);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX cache_entries_age;
    DROP INDEX cache_entries_expires;
    DROP POLICY worker_janitor_delete ON cache_entries;
    DROP POLICY worker_janitor_read ON cache_entries;
    REVOKE SELECT (id, org_id, expires_at, created_at), DELETE ON cache_entries FROM worker;
    TRUNCATE cache_entries;
    ALTER TABLE cache_entries DROP COLUMN key_id, DROP COLUMN wrapped_key, DROP COLUMN ciphertext;
    ALTER TABLE cache_entries ADD COLUMN response jsonb NOT NULL;
    CREATE POLICY worker_all ON cache_entries FOR ALL TO worker USING (true) WITH CHECK (true);
    GRANT SELECT, DELETE ON cache_entries TO worker;
    """)
