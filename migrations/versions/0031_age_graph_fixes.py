"""Apache AGE functions (migration 0030, ADR 0010), fixed after their first run in CI.

- AGE names `graphid_ops`, and the operators of Cypher comparisons, without a schema. So the
  functions that create labels or run Cypher search `ag_catalog` after `pg_catalog`. The schema
  belongs to the extension's owner, a superuser, and no other role can create objects in it.
- `cypher()` accepts its query only as a dollar-quoted constant. The readers built it as a quoted
  literal; they now dollar-quote it. The text holds only uuids, checked kinds and numbers, so it
  contains no `$`.

Revision ID: 0031
Revises: 0030
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER FUNCTION graph_age_sync(uuid) SET search_path = pg_catalog, ag_catalog, pg_temp;

    CREATE OR REPLACE FUNCTION graph_age_hops(
      project uuid, ids uuid[], kinds text[], direction text, depth int
    ) RETURNS TABLE (vertex uuid, confidence float8)
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, ag_catalog, pg_temp
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
            FROM ag_catalog.cypher(%L, $cypher$%s$cypher$) AS c(%s)
          ) hit
          ORDER BY uid, weakest DESC
        $q$, weakest, g,
          format('MATCH %s WHERE a.uid IN %s%s RETURN %s',
                 pattern, public.graph_age_list(chunk::text[]), filters, outputs),
          cols);
      END LOOP;
    END
    $fn$;

    CREATE OR REPLACE FUNCTION graph_age_reach(
      project uuid, ids uuid[], kinds text[], direction text, max_depth int,
      min_confidence float8, lim int
    ) RETURNS TABLE (vertex uuid, depth int)
      LANGUAGE plpgsql STABLE SET search_path = pg_catalog, ag_catalog, pg_temp
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
            FROM ag_catalog.cypher(%L, $cypher$%s$cypher$) AS c(uid ag_catalog.agtype)
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
    """)


def downgrade() -> None:
    # The 0030 versions never ran (CI failed on the first); downgrading to 0030 keeps these,
    # and 0030's own downgrade drops them.
    pass
