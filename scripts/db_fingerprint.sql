-- Everything that makes two databases the same: what the restore drill and the pg_upgrade
-- rehearsal compare (scripts/restore_drill.sh, scripts/upgrade_rehearsal.sh). Run as the
-- superuser, to whom row security never applies:
--   psql -At -v ON_ERROR_STOP=1 -d <database> < scripts/db_fingerprint.sql
SELECT 'migration ' || version_num FROM alembic_version
UNION ALL
SELECT 'extension ' || extname || ' ' || extversion FROM pg_extension
UNION ALL
SELECT 'database ' || datacl::text FROM pg_database WHERE datname = current_database()
UNION ALL
SELECT 'schema ' || nspname || ' ' || nspowner::regrole || ' ' || coalesce(nspacl::text, '')
FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
UNION ALL
SELECT 'relation ' || c.oid::regclass || ' ' || c.relkind::text || ' ' || c.relowner::regrole
       || ' rls ' || c.relrowsecurity || ' forced ' || c.relforcerowsecurity
       || ' ' || coalesce(c.relacl::text, '')
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
UNION ALL
SELECT 'column ' || a.attrelid::regclass || '.' || a.attname || ' ' || a.attacl::text
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL
UNION ALL
SELECT 'index ' || pg_get_indexdef(i.indexrelid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
WHERE c.relnamespace = 'public'::regnamespace
UNION ALL
SELECT 'constraint ' || conrelid::regclass || ' ' || conname || ' ' || pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = 'public'::regnamespace
UNION ALL
SELECT 'trigger ' || t.tgrelid::regclass || ' ' || t.tgname || ' ' || t.tgenabled::text
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
UNION ALL
SELECT 'function ' || p.oid::regprocedure || ' ' || p.prosecdef || ' ' || coalesce(p.proacl::text, '')
FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace
  AND NOT EXISTS (SELECT FROM pg_depend d
                  WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
UNION ALL
SELECT 'policy ' || tablename || ' ' || policyname || ' ' || cmd || ' '
       || array_to_string(roles, ',') || ' ' || coalesce(qual, '') || ' ' || coalesce(with_check, '')
FROM pg_policies WHERE schemaname = 'public'
UNION ALL
SELECT 'sequence ' || schemaname || '.' || sequencename || ' ' || coalesce(last_value::text, 'unused')
FROM pg_sequences WHERE schemaname !~ '^pg_'
UNION ALL
SELECT 'rows ' || c.oid::regclass || ' ' || (xpath('/row/n/text()', x))[1]::text
       || ' ' || (xpath('/row/h/text()', x))[1]::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL query_to_xml(format(
  'SELECT count(*) AS n, md5(coalesce(string_agg(md5(t::text), %L ORDER BY md5(t::text)), %L)) AS h FROM %s t',
  '', '', c.oid::regclass), false, true, '') AS x
WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
UNION ALL
SELECT 'ledger ' || org_id || ' ' || count(*) || ' events ' || sum(cost_micro_usd)
       || ' micro-usd ' || sum(input_tokens + output_tokens) || ' tokens'
FROM usage_events GROUP BY org_id
ORDER BY 1;
