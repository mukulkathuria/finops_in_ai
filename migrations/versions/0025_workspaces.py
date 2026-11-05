"""US-25: the agents' shared workspace (spec section 4, F6: the blackboard tables).

- `workspaces` are shared like projects (`acl_groups`); every read checks them in the same
  statement. A team key's workspace belongs to its team (`created_by_team`).
- `tasks` group agents' work; `agent_token_budget` caps each agent's discovery tokens on one.
- `task_agents` count the discovery tokens (broker reads, searches, clarification) per agent.
- `facts` are kept once per content in a workspace; `key` names a fact to get it by; their
  embeddings serve `search_workspace`, which is exact within a workspace (no global index).
- `file_summaries` hold one summary per path.
- `agent_messages` carry clarification between agents: one row per task, addressee and question,
  so a repeat is the same message, with the hop count of its chain.
- `task_queries` remember each agent's searches in a task, so a repeat is answered from them.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TABLES = (
    "workspaces",
    "tasks",
    "task_agents",
    "facts",
    "file_summaries",
    "agent_messages",
    "task_queries",
)


def upgrade() -> None:
    op.execute("""
    CREATE TABLE workspaces (
      id              uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      name            text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
      acl_groups      text[] NOT NULL CHECK (cardinality(acl_groups) BETWEEN 1 AND 200),
      created_by      uuid REFERENCES users (id) ON DELETE SET NULL,
      created_by_team uuid REFERENCES teams (id) ON DELETE SET NULL,
      created_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX workspaces_acl ON workspaces USING gin (acl_groups);

    CREATE TABLE tasks (
      id                 uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id             uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      workspace_id       uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
      title              text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
      agent_token_budget bigint CHECK (agent_token_budget > 0),
      created_at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX tasks_workspace ON tasks (workspace_id, id) INCLUDE (org_id);

    CREATE TABLE task_agents (
      task_id          uuid NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
      agent            text NOT NULL CHECK (char_length(agent) BETWEEN 1 AND 100),
      org_id           uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      discovery_tokens bigint NOT NULL DEFAULT 0 CHECK (discovery_tokens >= 0),
      PRIMARY KEY (task_id, agent)
    );

    CREATE TABLE facts (
      id              uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id          uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      workspace_id    uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
      task_id         uuid REFERENCES tasks (id) ON DELETE SET NULL,
      key             text CHECK (char_length(key) BETWEEN 1 AND 200),
      content         text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 20000),
      source          text CHECK (char_length(source) <= 1000),
      confidence      real NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
      agent           text NOT NULL CHECK (char_length(agent) BETWEEN 1 AND 100),
      content_hash    bytea NOT NULL CHECK (octet_length(content_hash) = 32),
      token_count     integer NOT NULL CHECK (token_count >= 0),
      embedding       vector(1024) NOT NULL,
      embedding_model text NOT NULL,
      created_at      timestamptz NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, content_hash)
    );
    CREATE INDEX facts_key ON facts (workspace_id, key, created_at DESC) INCLUDE (org_id)
      WHERE key IS NOT NULL;
    CREATE INDEX facts_workspace ON facts (workspace_id, embedding_model) INCLUDE (org_id);
    CREATE INDEX facts_task ON facts (task_id) WHERE task_id IS NOT NULL;

    CREATE TABLE file_summaries (
      id           uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id       uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      workspace_id uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
      path         text NOT NULL CHECK (char_length(path) BETWEEN 1 AND 1000),
      summary      text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 20000),
      agent        text NOT NULL CHECK (char_length(agent) BETWEEN 1 AND 100),
      token_count  integer NOT NULL CHECK (token_count >= 0),
      updated_at   timestamptz NOT NULL DEFAULT now(),
      UNIQUE (workspace_id, path)
    );

    CREATE TABLE agent_messages (
      id            uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      task_id       uuid NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
      from_agent    text NOT NULL CHECK (char_length(from_agent) BETWEEN 1 AND 100),
      to_agent      text NOT NULL CHECK (char_length(to_agent) BETWEEN 1 AND 100),
      question      text NOT NULL CHECK (char_length(question) BETWEEN 1 AND 4000),
      question_hash bytea NOT NULL CHECK (octet_length(question_hash) = 32),
      answer        text CHECK (char_length(answer) BETWEEN 1 AND 20000),
      hops          integer NOT NULL CHECK (hops BETWEEN 1 AND 10),
      asked         integer NOT NULL DEFAULT 1 CHECK (asked >= 1),
      created_at    timestamptz NOT NULL DEFAULT now(),
      answered_at   timestamptz,
      UNIQUE (task_id, to_agent, question_hash)
    );
    CREATE INDEX agent_messages_inbox ON agent_messages (task_id, to_agent) INCLUDE (org_id)
      WHERE answer IS NULL;
    CREATE INDEX agent_messages_asker ON agent_messages (task_id, from_agent) INCLUDE (org_id);

    CREATE TABLE task_queries (
      task_id    uuid NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
      agent      text NOT NULL CHECK (char_length(agent) BETWEEN 1 AND 100),
      query_hash bytea NOT NULL CHECK (octet_length(query_hash) = 32),
      org_id     uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      fact_ids   uuid[] NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (task_id, agent, query_hash)
    );
    """)
    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
        """)
    op.execute("""
    GRANT SELECT, INSERT ON workspaces, tasks, facts, task_queries TO app;
    GRANT SELECT, INSERT, UPDATE ON task_agents, file_summaries, agent_messages TO app;
    """)


def downgrade() -> None:
    op.execute(
        "DROP TABLE task_queries, agent_messages, file_summaries, facts, task_agents, tasks,"
        " workspaces;"
    )
