"""Phase 3: the price sync job (worker) appends model_catalog rows.

Rows are never updated or deleted, so the worker role gets INSERT only.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    GRANT INSERT ON model_catalog TO worker;
    DO $$
    BEGIN
      EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO worker', pg_get_serial_sequence('model_catalog', 'id')
      );
    END
    $$;
    """)


def downgrade() -> None:
    op.execute("""
    REVOKE INSERT ON model_catalog FROM worker;
    DO $$
    BEGIN
      EXECUTE format(
        'REVOKE USAGE ON SEQUENCE %s FROM worker', pg_get_serial_sequence('model_catalog', 'id')
      );
    END
    $$;
    """)
