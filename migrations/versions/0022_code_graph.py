"""US-22: projects and their code graph (spec section 5, "Code and knowledge graph").

- `projects` carry document-style ACL groups. Graph and context data inherit them: a caller who
  cannot read a project gets none of its vertices, edges or packs (every read checks the groups
  in the same statement).
- `graph_vertex` and `graph_edge` with covering indexes on both edge directions (the spec's
  speed plan). They lead with `src` and `dst` and include `org_id`, so they also serve foreign
  key cascades and the row-level security predicate. `context_packs` hold one precomputed pack
  per module: serving one is a single-row read.
- No SQL/PGQ property graph: PostgreSQL 19 ships without it (ADR 0004). `GraphStore` answers
  fixed-depth patterns with joins and variable depth with recursive CTEs.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLES = ("projects", "graph_vertex", "graph_edge", "context_packs")


def upgrade() -> None:
    op.execute("""
    CREATE TABLE projects (
      id          uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
      acl_groups  text[] NOT NULL CHECK (cardinality(acl_groups) BETWEEN 1 AND 200),
      created_by  uuid REFERENCES users (id) ON DELETE SET NULL,
      indexed_at  timestamptz,
      created_at  timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, name)
    );
    CREATE INDEX projects_acl ON projects USING gin (acl_groups);

    CREATE TABLE graph_vertex (
      id          uuid PRIMARY KEY,
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      project_id  uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
      kind        text NOT NULL CHECK (kind IN ('module', 'file', 'symbol', 'doc', 'owner')),
      name        text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 500),
      path        text CHECK (char_length(path) <= 1000),
      props       jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX graph_vertex_lookup ON graph_vertex (project_id, kind, name) INCLUDE (org_id);

    CREATE TABLE graph_edge (
      id          uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      project_id  uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
      src         uuid NOT NULL REFERENCES graph_vertex (id) ON DELETE CASCADE,
      dst         uuid NOT NULL REFERENCES graph_vertex (id) ON DELETE CASCADE,
      kind        text NOT NULL
                  CHECK (kind IN ('imports', 'calls', 'defines', 'documents', 'owns')),
      confidence  real NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1)
    );
    CREATE INDEX graph_edge_out ON graph_edge (src, kind) INCLUDE (dst, confidence, org_id);
    CREATE INDEX graph_edge_in ON graph_edge (dst, kind) INCLUDE (src, confidence, org_id);
    CREATE INDEX graph_edge_project ON graph_edge (project_id);

    CREATE TABLE context_packs (
      id            uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      project_id    uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
      module_id     uuid NOT NULL UNIQUE REFERENCES graph_vertex (id) ON DELETE CASCADE,
      path          text NOT NULL CHECK (char_length(path) BETWEEN 1 AND 1000),
      pack          text NOT NULL CHECK (char_length(pack) <= 200000),
      token_count   integer NOT NULL CHECK (token_count >= 0),
      source_tokens integer NOT NULL CHECK (source_tokens >= 0),
      refreshed_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX context_packs_project ON context_packs (project_id, path) INCLUDE (org_id);
    """)
    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
        """)
    op.execute("""
    GRANT SELECT, INSERT, UPDATE, DELETE ON projects TO app;
    GRANT SELECT, INSERT, DELETE ON graph_vertex, graph_edge, context_packs TO app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE context_packs, graph_edge, graph_vertex, projects;")
