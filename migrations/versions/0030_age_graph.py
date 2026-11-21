"""Apache AGE as an opt-in graph engine (ADR 0010): the functions that keep each project's AGE
graph and read it with Cypher.

Creating them needs nothing from AGE, since PL/pgSQL resolves names when a function runs. They
run only where `infra/docker/postgres/sql/age-setup.sql` installed the extension and the gateway
runs with GRAPH_ENGINE=age.

- `graph_age_sync(project)` rebuilds the project's AGE graph from `graph_vertex` and `graph_edge`
  in the caller's transaction, so readers see the old graph or the new one. It runs as its owner
  (`migrator`) because creating a graph creates a schema, which `app` may not. Row security still
  applies to it, so it sees the caller's org only.
- `graph_age_hops(...)` answers a fixed-depth pattern with one Cypher MATCH.
- `graph_age_reach(...)` walks breadth first, one Cypher hop per level, and stops once it has
  `lim` vertices: a deep walk never enumerates paths.

The readers return vertex ids only. The gateway joins them to `graph_vertex`, under row security,
after checking the project's ACL in the same statement.

Revision ID: 0030
Revises: 0029
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- One AGE graph per project: a walk cannot leave it, whatever AGE's traversal reads.
    CREATE FUNCTION graph_age_name(project uuid) RETURNS text
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      AS $$ SELECT 'g' || replace(project::text, '-', '') $$;

    -- A Cypher list of string literals. Callers pass uuids, or kinds they checked.
    CREATE FUNCTION graph_age_list(items text[]) RETURNS text
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      AS $$ SELECT '[' || string_agg(quote_literal(i), ', ') || ']' FROM unnest(items) i $$;

    CREATE FUNCTION graph_age_check(project uuid, kinds text[], direction text) RETURNS text
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_name(project);
    BEGIN
      IF direction IS NULL OR direction NOT IN ('out', 'in') THEN
        RAISE EXCEPTION 'direction is out or in' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      IF coalesce(cardinality(kinds), 0) = 0
         OR EXISTS (SELECT 1 FROM unnest(kinds) k WHERE k IS NULL OR k !~ '^[a-z]+$') THEN
        RAISE EXCEPTION 'edge kinds are lower-case words' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph a WHERE a.name = g) THEN
        RAISE EXCEPTION 'project % has no AGE graph; run infra/docker/postgres/sql/age-setup.sql',
          project USING ERRCODE = 'undefined_object';
      END IF;
      RETURN g;
    END
    $fn$;

    CREATE FUNCTION graph_age_sync(project uuid) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_name(project);
      t text;
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM public.projects p WHERE p.id = project) THEN
        RAISE EXCEPTION 'project % not found', project USING ERRCODE = 'no_data_found';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph a WHERE a.name = g) THEN
        EXECUTE format('SELECT ag_catalog.create_graph(%L)', g);
        EXECUTE format('SELECT ag_catalog.create_vlabel(%L, %L)', g, 'node');
        EXECUTE format('SELECT ag_catalog.create_elabel(%L, %L)', g, 'link');
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO app', g);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO app', g);
        -- Row security as on every other table: `app` reads a graph only while its project is
        -- visible to the session's org (projects has its own policy). The owner loads it.
        FOREACH t IN ARRAY ARRAY['node', 'link', '_ag_label_vertex', '_ag_label_edge'] LOOP
          EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', g, t);
          EXECUTE format('CREATE POLICY tenant_isolation ON %I.%I USING '
                         '(EXISTS (SELECT 1 FROM public.projects p WHERE p.id = %L))',
                         g, t, project);
        END LOOP;
      END IF;
      EXECUTE format('DELETE FROM %I.link', g);
      EXECUTE format('DELETE FROM %I.node', g);
      -- Ids are assigned here, not from the label sequences: the graph is only ever replaced
      -- whole, and no role is granted Cypher writes on it.
      EXECUTE format($q$
        WITH vs AS MATERIALIZED (
          SELECT v.id, v.kind, v.name, v.path,
                 ag_catalog._graphid(%s, row_number() OVER (ORDER BY v.id)) AS gid
          FROM public.graph_vertex v
          WHERE v.project_id = $1
        ), nodes AS (
          INSERT INTO %I.node (id, properties)
          SELECT gid, ag_catalog.agtype_build_map(
                   'uid', id::text, 'kind', kind, 'name', name, 'path', path)
          FROM vs
        )
        INSERT INTO %I.link (id, start_id, end_id, properties)
        SELECT ag_catalog._graphid(%s, row_number() OVER ()), s.gid, d.gid,
               ag_catalog.agtype_build_map('kind', e.kind, 'confidence', e.confidence::float8)
        FROM public.graph_edge e
        JOIN vs s ON s.id = e.src
        JOIN vs d ON d.id = e.dst
        WHERE e.project_id = $1
      $q$, ag_catalog._label_id(g, 'node'), g, g, ag_catalog._label_id(g, 'link'))
      USING project;
    END
    $fn$;

    -- The vertices exactly `depth` hops away, each with the weakest edge on each path. Cypher
    -- does not reuse an edge within one path; the join-based store does.
    CREATE FUNCTION graph_age_hops(
      project uuid, ids uuid[], kinds text[], direction text, depth int
    ) RETURNS TABLE (vertex uuid, confidence float8)
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_check(project, kinds, direction);
      kind_list text := public.graph_age_list(kinds);
      pattern text := '(a:node)';
      filters text := '';
      outputs text := 'b.uid';
      cols text := 'uid ag_catalog.agtype';
      weakest text := '';
      chunk uuid[];
      i int;
    BEGIN
      IF depth IS NULL OR depth NOT BETWEEN 1 AND 3 THEN
        RAISE EXCEPTION 'depth is between 1 and 3' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      FOR i IN 1..depth LOOP
        pattern := pattern
          || CASE direction WHEN 'out' THEN format('-[e%s:link]->', i)
                            ELSE format('<-[e%s:link]-', i) END
          || CASE WHEN i = depth THEN '(b:node)' ELSE format('(n%s:node)', i) END;
        filters := filters || format(' AND e%s.kind IN %s', i, kind_list);
        outputs := outputs || format(', e%s.confidence', i);
        cols := cols || format(', c%s ag_catalog.agtype', i);
        weakest := weakest || CASE WHEN i > 1 THEN ', ' ELSE '' END
          || format('CAST(c.c%s AS float8)', i);
      END LOOP;
      FOR i IN 0..(coalesce(cardinality(ids), 0) - 1) / 1000 LOOP
        chunk := ids[i * 1000 + 1 : (i + 1) * 1000];
        CONTINUE WHEN coalesce(cardinality(chunk), 0) = 0;
        RETURN QUERY EXECUTE format($q$
          SELECT DISTINCT ON (uid) uid, weakest FROM (
            SELECT btrim(CAST(c.uid AS text), '"')::uuid AS uid, least(%s) AS weakest
            FROM ag_catalog.cypher(%L, %L) AS c(%s)
          ) hit
          ORDER BY uid, weakest DESC
        $q$, weakest, g,
          format('MATCH %s WHERE a.uid IN %s%s RETURN %s',
                 pattern, public.graph_age_list(chunk::text[]), filters, outputs),
          cols);
      END LOOP;
    END
    $fn$;

    -- Every vertex within `max_depth` hops along edges at least as sure as `min_confidence`, at
    -- its shortest depth, breadth first. It stops after the level that reaches `lim` vertices:
    -- ordered by depth, nothing deeper can make the caller's limit.
    CREATE FUNCTION graph_age_reach(
      project uuid, ids uuid[], kinds text[], direction text, max_depth int,
      min_confidence float8, lim int
    ) RETURNS TABLE (vertex uuid, depth int)
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp
      AS $fn$
    DECLARE
      g text := public.graph_age_check(project, kinds, direction);
      kind_list text := public.graph_age_list(kinds);
      hop text := CASE direction WHEN 'out' THEN '(a:node)-[e:link]->(b:node)'
                                 ELSE '(a:node)<-[e:link]-(b:node)' END;
      seen uuid[] := '{}';
      frontier uuid[] := coalesce(ids, '{}');
      reached uuid[];
      part uuid[];
      chunk uuid[];
      step int;
      i int;
    BEGIN
      IF max_depth IS NULL OR max_depth NOT BETWEEN 1 AND 8 THEN
        RAISE EXCEPTION 'max_depth is between 1 and 8' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      IF min_confidence IS NULL OR min_confidence NOT BETWEEN 0 AND 1 THEN
        RAISE EXCEPTION 'min_confidence is between 0 and 1'
          USING ERRCODE = 'invalid_parameter_value';
      END IF;
      IF lim IS NULL OR lim < 1 THEN
        RAISE EXCEPTION 'lim is positive' USING ERRCODE = 'invalid_parameter_value';
      END IF;
      FOR step IN 1..max_depth LOOP
        EXIT WHEN cardinality(frontier) = 0;
        reached := '{}';
        FOR i IN 0..(cardinality(frontier) - 1) / 1000 LOOP
          chunk := frontier[i * 1000 + 1 : (i + 1) * 1000];
          EXECUTE format($q$
            SELECT array_agg(DISTINCT btrim(CAST(c.uid AS text), '"')::uuid)
            FROM ag_catalog.cypher(%L, %L) AS c(uid ag_catalog.agtype)
          $q$, g,
            format('MATCH %s WHERE a.uid IN %s AND e.kind IN %s AND e.confidence >= %s '
                   'RETURN DISTINCT b.uid',
                   hop, public.graph_age_list(chunk::text[]), kind_list,
                   round(min_confidence::numeric, 6)))  -- fixed-point, never 1e-05
          INTO part;
          reached := reached || coalesce(part, '{}');
        END LOOP;
        frontier := ARRAY(SELECT unnest(reached) EXCEPT SELECT unnest(seen));
        seen := seen || frontier;
        RETURN QUERY SELECT f, step FROM unnest(frontier) f;
        EXIT WHEN cardinality(seen) >= lim;
      END LOOP;
    END
    $fn$;

    REVOKE ALL ON FUNCTION graph_age_name(uuid), graph_age_list(text[]),
      graph_age_check(uuid, text[], text), graph_age_sync(uuid),
      graph_age_hops(uuid, uuid[], text[], text, int),
      graph_age_reach(uuid, uuid[], text[], text, int, float8, int) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION graph_age_name(uuid), graph_age_list(text[]),
      graph_age_check(uuid, text[], text), graph_age_sync(uuid),
      graph_age_hops(uuid, uuid[], text[], text, int),
      graph_age_reach(uuid, uuid[], text[], text, int, float8, int) TO app;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION graph_age_reach(uuid, uuid[], text[], text, int, float8, int),
      graph_age_hops(uuid, uuid[], text[], text, int), graph_age_sync(uuid),
      graph_age_check(uuid, text[], text), graph_age_list(text[]), graph_age_name(uuid);
    """)
