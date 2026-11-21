-- Apache AGE for the code graph (ADR 0010), opt-in. Run as a superuser while connected to the
-- application database, after the migrations, then restart the gateway with GRAPH_ENGINE=age:
--   docker compose exec db psql -U postgres -d finops -f /opt/finops/sql/age-setup.sql
-- Idempotent. Run it again after a restore or a major upgrade: it rebuilds every project's graph
-- from graph_vertex and graph_edge, which stay the source of truth.
CREATE EXTENSION IF NOT EXISTS age;

GRANT USAGE ON SCHEMA ag_catalog TO app, migrator;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO app, migrator;

DO $$ BEGIN
  -- graph_age_sync (migration 0030, owned by migrator) creates one schema per project graph.
  EXECUTE format('GRANT CREATE ON DATABASE %I TO migrator', current_database());
  -- AGE's parser hook turns cypher() calls into plans, so the gateway's sessions load it.
  EXECUTE format('ALTER ROLE app IN DATABASE %I SET session_preload_libraries = %L',
                 current_database(), 'age');
END $$;

-- Every project's code graph and every workspace's communication graph (migration 0032), each
-- under its own org's row security.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.id, p.org_id FROM public.projects p ORDER BY p.id LOOP
    PERFORM set_config('app.org_id', r.org_id::text, true);
    PERFORM public.graph_age_sync(r.id);
  END LOOP;
  FOR r IN SELECT w.id, w.org_id FROM public.workspaces w ORDER BY w.id LOOP
    PERFORM set_config('app.org_id', r.org_id::text, true);
    PERFORM public.graph_age_workspace_put(r.id, NULL);
  END LOOP;
  PERFORM set_config('app.org_id', '', true);
END $$;
