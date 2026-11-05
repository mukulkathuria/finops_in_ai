"""Phase 4 review (US-22): project names unique per owner, and edge indexes that answer the
project filter without a heap fetch.

- `projects`: `UNIQUE (org_id, name)` let anyone in the org learn, from a refused create, the
  name of a project they cannot read (review S-L2). A name is now unique per owner: the user who
  created the project, or the team whose key did.
- `graph_edge_out` and `graph_edge_in` also cover `project_id`, so the first hop of a walk is
  filtered inside the index (review C-L7).

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE projects DROP CONSTRAINT projects_org_id_name_key;
    CREATE UNIQUE INDEX projects_owner_name
      ON projects (org_id, (coalesce(created_by, created_by_team)), name);
    DROP INDEX graph_edge_out;
    DROP INDEX graph_edge_in;
    CREATE INDEX graph_edge_out ON graph_edge (src, kind)
      INCLUDE (dst, confidence, org_id, project_id);
    CREATE INDEX graph_edge_in ON graph_edge (dst, kind)
      INCLUDE (src, confidence, org_id, project_id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX graph_edge_in;
    DROP INDEX graph_edge_out;
    CREATE INDEX graph_edge_out ON graph_edge (src, kind) INCLUDE (dst, confidence, org_id);
    CREATE INDEX graph_edge_in ON graph_edge (dst, kind) INCLUDE (src, confidence, org_id);
    DROP INDEX projects_owner_name;
    ALTER TABLE projects ADD CONSTRAINT projects_org_id_name_key UNIQUE (org_id, name);
    """)
