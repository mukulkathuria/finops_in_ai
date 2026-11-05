"""Release review: the worker reads budgets and budget requests and never writes them.

The janitor rebuilds counters (budget_usage) from the ledger and returns expired holds
(budget_reservations). It only reads the budgets themselves, and never touches budget requests.
Its old full-table write grants would have let a compromised worker raise any org's limits.

Revision ID: 0029
Revises: 0028
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON budgets, budget_requests FROM worker")


def downgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON budgets, budget_requests TO worker")
