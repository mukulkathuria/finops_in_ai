-- Cluster-level roles (psql script; passwords passed as psql variables).
-- migrator: owns schema objects, runs migrations. app: request path, always under RLS.
-- worker: background jobs, cross-tenant only through explicit per-table policies.
-- readonly_analytics: read-only reporting, still under tenant RLS.
SELECT format('CREATE ROLE migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'migrator_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'migrator') \gexec
SELECT format('CREATE ROLE app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'app_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') \gexec
SELECT format('CREATE ROLE worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'worker_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'worker') \gexec
SELECT format('CREATE ROLE readonly_analytics LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'readonly_pw')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'readonly_analytics') \gexec
