"""Phase 2 review follow-ups: the app may delete parse artifacts nobody references any more
(a file's derived Markdown goes with its last copy), and indexes for that lookup and for exact
vector search within one org.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    GRANT DELETE ON file_artifacts TO app;
    CREATE INDEX files_sha256 ON files (org_id, sha256);
    CREATE INDEX chunks_org ON chunks (org_id, embedding_model);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX chunks_org;
    DROP INDEX files_sha256;
    REVOKE DELETE ON file_artifacts FROM app;
    """)
