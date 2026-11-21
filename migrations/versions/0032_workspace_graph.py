"""The agents' communication as a graph (US-25, ADR 0010).

A workspace is a graph of its tasks, agents, questions and facts:
- `member_of` (agent to task);
- `asked` (agent to question) and `asked_of` (question to the agent asked);
- `answered` (agent to question);
- `replies_to` (question to the question it follows up);
- `wrote` (agent to a fact it recorded in a task).

An agent is a vertex per task, since a name belongs to one member within a task (0027).
- `agent_messages.reply_to` keeps the follow-up link, where only the hop count was kept before.
- The views `workspace_graph_vertex` and `workspace_graph_edge` are the graph over the tables,
  which keep their caps and dedup rules. They are security-invoker views, so row security applies
  to whoever reads them.
- With Apache AGE (GRAPH_ENGINE=age) each workspace is also an AGE graph, `w` followed by the
  workspace id's hex:
  - `graph_age_workspace_put` adds the vertices and edges of a write, in its transaction;
  - `graph_age_workspace_drop` removes the graph with its workspace;
  - `graph_age_task_context` walks it with Cypher, one MATCH per hop.

Revision ID: 0032
Revises: 0031
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE agent_messages
      ADD COLUMN reply_to uuid REFERENCES agent_messages (id) ON DELETE SET NULL;
    CREATE INDEX agent_messages_reply_to ON agent_messages (reply_to) WHERE reply_to IS NOT NULL;

    -- An agent's vertex: its name within one task. Names cannot hold '/' (AGENT_PATTERN).
    CREATE FUNCTION workspace_agent_id(task uuid, agent text) RETURNS uuid
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      AS $$ SELECT md5(task::text || '/' || agent)::uuid $$;

    CREATE VIEW workspace_graph_vertex WITH (security_invoker = true) AS
      SELECT t.workspace_id, t.id AS task_id, t.id, 'task'::text AS kind, t.title AS name
      FROM tasks t
      UNION ALL
      SELECT t.workspace_id, a.task_id, workspace_agent_id(a.task_id, a.agent), 'agent', a.agent
      FROM task_agents a JOIN tasks t ON t.id = a.task_id
      UNION ALL
      SELECT t.workspace_id, m.task_id, m.id, 'question', NULL
      FROM agent_messages m JOIN tasks t ON t.id = m.task_id
      UNION ALL
      SELECT f.workspace_id, f.task_id, f.id, 'fact', f.key
      FROM facts f;

    CREATE VIEW workspace_graph_edge WITH (security_invoker = true) AS
      SELECT t.workspace_id, a.task_id, workspace_agent_id(a.task_id, a.agent) AS src,
             a.task_id AS dst, 'member_of'::text AS kind
      FROM task_agents a JOIN tasks t ON t.id = a.task_id
      UNION ALL
      SELECT t.workspace_id, m.task_id, workspace_agent_id(m.task_id, m.from_agent), m.id, 'asked'
      FROM agent_messages m JOIN tasks t ON t.id = m.task_id
      UNION ALL
      SELECT t.workspace_id, m.task_id, m.id, workspace_agent_id(m.task_id, m.to_agent), 'asked_of'
      FROM agent_messages m JOIN tasks t ON t.id = m.task_id
      UNION ALL
      SELECT t.workspace_id, m.task_id, workspace_agent_id(m.task_id, m.to_agent), m.id, 'answered'
      FROM agent_messages m JOIN tasks t ON t.id = m.task_id
      WHERE m.answer IS NOT NULL
      UNION ALL
      SELECT t.workspace_id, m.task_id, m.id, m.reply_to, 'replies_to'
      FROM agent_messages m JOIN tasks t ON t.id = m.task_id
      WHERE m.reply_to IS NOT NULL
      UNION ALL
      SELECT f.workspace_id, f.task_id, workspace_agent_id(f.task_id, f.agent), f.id, 'wrote'
      FROM facts f
      WHERE f.task_id IS NOT NULL;

    CREATE FUNCTION graph_age_workspace_name(workspace uuid) RETURNS text
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      AS $$ SELECT 'w' || replace(workspace::text, '-', '') $$;

    -- The vertices of `vertex_kind` one edge of `kinds` away from `ids`, either way.
    CREATE FUNCTION graph_age_step(g text, ids uuid[], kinds text[], vertex_kind text)
      RETURNS uuid[]
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, ag_catalog, pg_temp
      AS $fn$
    DECLARE
      hits uuid[] := '{}';
      part uuid[];
      chunk uuid[];
      i int;
    BEGIN
      IF vertex_kind IS NULL OR vertex_kind !~ '^[a-z_]+$'
         OR coalesce(cardinality(kinds), 0) = 0
         OR EXISTS (SELECT 1 FROM unnest(kinds) k WHERE k IS NULL OR k !~ '^[a-z_]+$') THEN
        RAISE EXCEPTION 'kinds are lower-case words' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      IF coalesce(cardinality(ids), 0) = 0 THEN
        RETURN '{}';
      END IF;
      FOR i IN 0..(cardinality(ids) - 1) / 1000 LOOP
        chunk := ids[i * 1000 + 1 : (i + 1) * 1000];
        EXECUTE format($q$
          SELECT array_agg(DISTINCT btrim(CAST(c.uid AS text), '"')::uuid)
          FROM ag_catalog.cypher(%L, $cypher$%s$cypher$) AS c(uid ag_catalog.agtype)
        $q$, g,
          format('MATCH (a:node)-[e:link]-(b:node) WHERE a.uid IN %s AND e.kind IN %s'
                 ' AND b.kind = %L RETURN DISTINCT b.uid',
                 public.graph_age_list(chunk::text[]), public.graph_age_list(kinds), vertex_kind))
        INTO part;
        hits := hits || coalesce(part, '{}');
      END LOOP;
      RETURN ARRAY(SELECT DISTINCT unnest(hits));
    END
    $fn$;

    -- Adds `ids` (NULL: every vertex of the workspace) to its AGE graph, with the far ends of
    -- their edges and every edge between vertices it has. The last statement of each write's
    -- transaction: the lock below is then never held while waiting for another.
    CREATE FUNCTION graph_age_workspace_put(workspace uuid, ids uuid[]) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, ag_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_workspace_name(workspace);
      t text;
      wanted uuid[];
      created uuid[];
      vseq text;
      eseq text;
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace) THEN
        RAISE EXCEPTION 'workspace % not found', workspace USING ERRCODE = 'no_data_found';
      END IF;
      PERFORM pg_advisory_xact_lock(hashtextextended(g, 0));  -- one writer per graph
      IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph a WHERE a.name = g) THEN
        EXECUTE format('SELECT ag_catalog.create_graph(%L)', g);
        EXECUTE format('SELECT ag_catalog.create_vlabel(%L, %L)', g, 'node');
        EXECUTE format('SELECT ag_catalog.create_elabel(%L, %L)', g, 'link');
        EXECUTE format('CREATE TABLE %I.uid_map (uid uuid PRIMARY KEY, gid graphid NOT NULL)', g);
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO app', g);
        FOREACH t IN ARRAY ARRAY['node', 'link', '_ag_label_vertex', '_ag_label_edge'] LOOP
          EXECUTE format('GRANT SELECT ON %I.%I TO app', g, t);
          EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', g, t);
          EXECUTE format('CREATE POLICY tenant_isolation ON %I.%I USING '
                         '(EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = %L))',
                         g, t, workspace);
        END LOOP;
      END IF;
      IF ids IS NULL THEN
        ids := ARRAY(SELECT v.id FROM public.workspace_graph_vertex v
                     WHERE v.workspace_id = workspace);
      END IF;
      wanted := ARRAY(
        SELECT unnest(ids)
        UNION SELECT e.src FROM public.workspace_graph_edge e
              WHERE e.workspace_id = workspace AND e.dst = ANY(ids)
        UNION SELECT e.dst FROM public.workspace_graph_edge e
              WHERE e.workspace_id = workspace AND e.src = ANY(ids));
      SELECT l.seq_name INTO vseq FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph a
        ON a.graphid = l.graph WHERE a.name = g AND l.name = 'node';
      SELECT l.seq_name INTO eseq FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph a
        ON a.graphid = l.graph WHERE a.name = g AND l.name = 'link';
      EXECUTE format($q$
        WITH fresh AS MATERIALIZED (
          SELECT v.id, v.kind, v.name, ag_catalog._graphid(%s, nextval(%L)) AS gid
          FROM public.workspace_graph_vertex v
          WHERE v.workspace_id = $1 AND v.id = ANY($2)
            AND NOT EXISTS (SELECT 1 FROM %I.uid_map m WHERE m.uid = v.id)
        ), mapped AS (
          INSERT INTO %I.uid_map (uid, gid) SELECT id, gid FROM fresh
        ), nodes AS (
          INSERT INTO %I.node (id, properties)
          SELECT gid, ag_catalog.agtype_build_map('uid', id::text, 'kind', kind, 'name', name)
          FROM fresh
        )
        SELECT array_agg(id) FROM fresh
      $q$, ag_catalog._label_id(g, 'node'), format('%I.%I', g, vseq), g, g, g)
      INTO created USING workspace, wanted;
      EXECUTE format($q$
        INSERT INTO %I.link (id, start_id, end_id, properties)
        SELECT ag_catalog._graphid(%s, nextval(%L)), s.gid, d.gid,
               ag_catalog.agtype_build_map('kind', e.kind)
        FROM public.workspace_graph_edge e
        JOIN %I.uid_map s ON s.uid = e.src
        JOIN %I.uid_map d ON d.uid = e.dst
        WHERE e.workspace_id = $1 AND (e.src = ANY($2) OR e.dst = ANY($2))
          AND NOT EXISTS (SELECT 1 FROM %I.link l
                          WHERE l.start_id = s.gid AND l.end_id = d.gid
                            AND l.properties = ag_catalog.agtype_build_map('kind', e.kind))
      $q$, g, ag_catalog._label_id(g, 'link'), format('%I.%I', g, eseq), g, g, g)
      USING workspace, ids || coalesce(created, '{}');
    END
    $fn$;

    -- Called before the workspace's own delete, in its transaction: a refused delete rolls the
    -- drop back with it.
    CREATE FUNCTION graph_age_workspace_drop(workspace uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, ag_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_workspace_name(workspace);
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = workspace) THEN
        RAISE EXCEPTION 'workspace % not found', workspace USING ERRCODE = 'no_data_found';
      END IF;
      IF EXISTS (SELECT 1 FROM ag_catalog.ag_graph a WHERE a.name = g) THEN
        EXECUTE format('SELECT ag_catalog.drop_graph(%L, true)', g);
      END IF;
    END
    $fn$;

    -- An agent's context in a task, by the graph: the questions it asked or was asked, their
    -- threads (replies either way; a thread is at most MAX_HOPS deep, so 4 steps cross it), and
    -- the facts the agents of those threads, itself included, recorded in the task.
    CREATE FUNCTION graph_age_task_context(workspace uuid, task uuid, agent text)
      RETURNS TABLE (kind text, vertex uuid)
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, ag_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_workspace_name(workspace);
      me uuid := public.workspace_agent_id(task, agent);
      questions uuid[];
      frontier uuid[];
      peers uuid[];
      step int;
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph a WHERE a.name = g) THEN
        RAISE EXCEPTION 'workspace % has no AGE graph; run infra/docker/postgres/sql/age-setup.sql',
          workspace USING ERRCODE = 'undefined_object';
      END IF;
      questions := public.graph_age_step(g, ARRAY[me], ARRAY['asked', 'asked_of'], 'question');
      frontier := questions;
      FOR step IN 1..4 LOOP
        EXIT WHEN cardinality(frontier) = 0;
        frontier := ARRAY(
          SELECT unnest(public.graph_age_step(g, frontier, ARRAY['replies_to'], 'question'))
          EXCEPT SELECT unnest(questions));
        questions := questions || frontier;
      END LOOP;
      peers := ARRAY[me]
        || public.graph_age_step(g, questions, ARRAY['asked', 'asked_of'], 'agent');
      RETURN QUERY
        SELECT 'question'::text, q FROM unnest(questions) q
        UNION ALL
        SELECT 'fact'::text, f
        FROM unnest(public.graph_age_step(g, peers, ARRAY['wrote'], 'fact')) f;
    END
    $fn$;

    REVOKE ALL ON FUNCTION workspace_agent_id(uuid, text), graph_age_workspace_name(uuid),
      graph_age_step(text, uuid[], text[], text), graph_age_workspace_put(uuid, uuid[]),
      graph_age_workspace_drop(uuid), graph_age_task_context(uuid, uuid, text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION workspace_agent_id(uuid, text), graph_age_workspace_name(uuid),
      graph_age_step(text, uuid[], text[], text), graph_age_workspace_put(uuid, uuid[]),
      graph_age_workspace_drop(uuid), graph_age_task_context(uuid, uuid, text) TO app;
    GRANT SELECT ON workspace_graph_vertex, workspace_graph_edge TO app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION graph_age_task_context(uuid, uuid, text), graph_age_workspace_drop(uuid),
      graph_age_workspace_put(uuid, uuid[]), graph_age_step(text, uuid[], text[], text),
      graph_age_workspace_name(uuid);
    DROP VIEW workspace_graph_edge, workspace_graph_vertex;
    DROP FUNCTION workspace_agent_id(uuid, text);
    ALTER TABLE agent_messages DROP COLUMN reply_to;
    """)
