"""Graph retrieval for knowledge domains (US-21 follow-up, ADR 0011).

- `knowledge_domains`: what an org declares its graph is for (say `medical`). Each domain has its
  entity and relation types, and example questions whose mean embedding is the domain's intent.
  A query whose embedding is near enough to an intent, and that names the domain's entities, is
  answered from the graph.
- A document is extracted into the graph only when it is filed under a domain
  (`documents.domain_id`). The worker extracts it in the background and keeps its
  `graph_status`: pending, running, ready or failed.
- `kg_entities`, `kg_relations` and `kg_evidence`: the graph. Every relation keeps the chunks it
  was read from, so a reader gets only the relations of documents it may read, and the passages
  sent to a model are those chunks.
- The worker reads documents and chunks across orgs, and writes the graph (its own policies, as
  for the outbox; its table grants bound what it does).

Revision ID: 0033
Revises: 0032
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

TABLES = ("knowledge_domains", "kg_entities", "kg_relations", "kg_evidence")


def upgrade() -> None:
    op.execute("""
    CREATE TABLE knowledge_domains (
      id               uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id           uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      name             text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{0,62}$'),
      description      text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 2000),
      entity_types     text[] NOT NULL CHECK (cardinality(entity_types) BETWEEN 1 AND 30),
      relation_types   text[] NOT NULL CHECK (cardinality(relation_types) BETWEEN 1 AND 30),
      examples         text[] NOT NULL CHECK (cardinality(examples) BETWEEN 1 AND 50),
      intent_threshold real NOT NULL CHECK (intent_threshold BETWEEN 0 AND 1),
      intent_embedding vector(1024) NOT NULL,
      embedding_model  text NOT NULL,
      created_by       uuid REFERENCES users (id) ON DELETE SET NULL,
      created_at       timestamptz NOT NULL DEFAULT now(),
      UNIQUE (org_id, name)
    );

    ALTER TABLE documents
      ADD COLUMN domain_id uuid REFERENCES knowledge_domains (id) ON DELETE SET NULL,
      ADD COLUMN graph_status text NOT NULL DEFAULT 'none'
        CHECK (graph_status IN ('none', 'pending', 'running', 'ready', 'failed')),
      ADD COLUMN graph_claimed_at timestamptz,
      ADD COLUMN graph_error text CHECK (char_length(graph_error) <= 1000),
      ADD COLUMN graph_tokens integer NOT NULL DEFAULT 0 CHECK (graph_tokens >= 0);
    CREATE INDEX documents_graph_queue ON documents (created_at)
      WHERE graph_status IN ('pending', 'running');

    CREATE TABLE kg_entities (
      id        uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id    uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      domain_id uuid NOT NULL REFERENCES knowledge_domains (id) ON DELETE CASCADE,
      type      text NOT NULL CHECK (char_length(type) BETWEEN 1 AND 60),
      name      text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
      norm      text NOT NULL CHECK (char_length(norm) BETWEEN 1 AND 200),
      UNIQUE (domain_id, type, norm)
    );
    CREATE INDEX kg_entities_norm ON kg_entities (domain_id, norm) INCLUDE (org_id);

    CREATE TABLE kg_relations (
      id        uuid PRIMARY KEY DEFAULT uuidv7(),
      org_id    uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      domain_id uuid NOT NULL REFERENCES knowledge_domains (id) ON DELETE CASCADE,
      src       uuid NOT NULL REFERENCES kg_entities (id) ON DELETE CASCADE,
      dst       uuid NOT NULL REFERENCES kg_entities (id) ON DELETE CASCADE,
      type      text NOT NULL CHECK (char_length(type) BETWEEN 1 AND 60),
      UNIQUE (src, dst, type)
    );
    CREATE INDEX kg_relations_dst ON kg_relations (dst) INCLUDE (src, type, org_id);

    CREATE TABLE kg_evidence (
      relation_id uuid NOT NULL REFERENCES kg_relations (id) ON DELETE CASCADE,
      chunk_id    uuid NOT NULL,
      document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
      org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
      PRIMARY KEY (relation_id, chunk_id)
    );
    CREATE INDEX kg_evidence_document ON kg_evidence (document_id);
    CREATE INDEX kg_evidence_chunk ON kg_evidence (chunk_id);
    """)
    for table in TABLES:
        op.execute(f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
          USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
        CREATE POLICY worker_graph ON {table} FOR ALL TO worker USING (true);
        """)
    op.execute("""
    CREATE POLICY worker_graph ON documents FOR ALL TO worker USING (true);
    CREATE POLICY worker_graph ON chunks FOR SELECT TO worker USING (true);
    GRANT SELECT, INSERT, DELETE ON knowledge_domains TO app;
    GRANT SELECT ON kg_entities, kg_relations, kg_evidence TO app;
    GRANT SELECT ON knowledge_domains, documents, chunks TO worker;
    GRANT UPDATE (graph_status, graph_claimed_at, graph_error, graph_tokens) ON documents TO worker;
    GRANT SELECT, INSERT, UPDATE, DELETE ON kg_entities, kg_relations, kg_evidence TO worker;
    """)


def downgrade() -> None:
    op.execute("""
    REVOKE SELECT ON documents, chunks FROM worker;
    REVOKE UPDATE (graph_status, graph_claimed_at, graph_error, graph_tokens) ON documents
      FROM worker;
    DROP POLICY worker_graph ON chunks;
    DROP POLICY worker_graph ON documents;
    DROP TABLE kg_evidence, kg_relations, kg_entities;
    ALTER TABLE documents DROP COLUMN graph_tokens, DROP COLUMN graph_error,
      DROP COLUMN graph_claimed_at, DROP COLUMN graph_status, DROP COLUMN domain_id;
    DROP TABLE knowledge_domains;
    """)
