"""Phase 5 review (US-25): bounds, identities and deletion for the agents' workspace.

- `workspaces.fact_count` counts a workspace's facts. `put_fact` raises it in the statement
  that checks the cap, so concurrent writers cannot pass it; a remembered search keeps the count
  it saw (`task_queries.fact_count`), so a repeat after new facts runs again.
- `task_agents.principal` binds an agent's name in a task to the member that first used it: the
  user, or the team of a team key. Another member cannot read its inbox, answer its questions
  or spend its discovery budget.
- `written_by` (facts, file summaries, questions) and `answered_by` record the member behind
  each agent's words, whatever name the agent gave.
- The app may delete a workspace (its creator or an admin decide); tasks, facts, summaries,
  questions and remembered searches go with it.

The backfill of `fact_count` lifts forced row-level security as migration 0026 does: a
migration session has no org, and the tenant policy would hide every fact from it.

Revision ID: 0027
Revises: 0026
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE workspaces
      ADD COLUMN fact_count integer NOT NULL DEFAULT 0 CHECK (fact_count >= 0);
    ALTER TABLE task_queries ADD COLUMN fact_count integer NOT NULL DEFAULT 0;
    ALTER TABLE task_agents ADD COLUMN principal uuid;
    ALTER TABLE facts ADD COLUMN written_by uuid;
    ALTER TABLE file_summaries ADD COLUMN written_by uuid;
    ALTER TABLE agent_messages ADD COLUMN written_by uuid, ADD COLUMN answered_by uuid;

    ALTER TABLE workspaces NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE facts NO FORCE ROW LEVEL SECURITY;
    UPDATE workspaces w
       SET fact_count = (SELECT count(*) FROM facts f WHERE f.workspace_id = w.id);
    ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
    ALTER TABLE facts FORCE ROW LEVEL SECURITY;

    GRANT UPDATE (fact_count) ON workspaces TO app;
    GRANT DELETE ON workspaces TO app;
    GRANT UPDATE (fact_ids, fact_count, created_at) ON task_queries TO app;
    """)


def downgrade() -> None:
    op.execute("""
    REVOKE DELETE ON workspaces FROM app;
    REVOKE UPDATE (fact_ids, created_at) ON task_queries FROM app;
    ALTER TABLE agent_messages DROP COLUMN answered_by, DROP COLUMN written_by;
    ALTER TABLE file_summaries DROP COLUMN written_by;
    ALTER TABLE facts DROP COLUMN written_by;
    ALTER TABLE task_agents DROP COLUMN principal;
    ALTER TABLE task_queries DROP COLUMN fact_count;
    ALTER TABLE workspaces DROP COLUMN fact_count;
    """)
