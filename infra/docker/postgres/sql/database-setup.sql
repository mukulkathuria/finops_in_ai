-- Database-level setup. Run as a superuser while connected to the target database.
-- Idempotent; shared by the container bootstrap and the integration-test harness.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO migrator;
GRANT USAGE ON SCHEMA public TO app, worker, readonly_analytics;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;
-- pg_partman non-superuser setup (per its docs): migrator manages partition sets.
GRANT ALL ON SCHEMA partman TO migrator;
GRANT ALL ON ALL TABLES IN SCHEMA partman TO migrator;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO migrator;
GRANT EXECUTE ON ALL PROCEDURES IN SCHEMA partman TO migrator;
DO $$ BEGIN
  EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO migrator', current_database());
END $$;
