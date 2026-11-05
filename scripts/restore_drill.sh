#!/usr/bin/env bash
# Restore drill (spec section 10: "restore the database from backup into a clean environment and
# verify the ledger totals"). Backs up the stack's database with pg_dump and restores the backup
# into a new database, made the way docs/runbooks/restore.md makes one. Then proves the copy whole:
# - the same migration, extensions, grants, functions, indexes, constraints and triggers;
# - the same forced row-level security and policies;
# - the same rows in every table, by count and by content hash;
# - the same ledger totals for every org.
# The client tools run inside the database container, so nothing needs installing.
#
#   scripts/restore_drill.sh                    the compose stack's `db` service, database `finops`
#   DB_CONTAINER=pg scripts/restore_drill.sh    a plain container of the database image (CI)
#
# Stop the writers first (docker compose stop gateway worker). The source is read again after the
# dump, so a write in between would show as a difference.
set -euo pipefail
cd "$(dirname "$0")/.."
service="${DB_SERVICE:-db}"
source_db="${DB_NAME:-finops}"
copy="${source_db}_restore_drill"
dump="/tmp/${source_db}.dump"

in_db() {
  if [ -n "${DB_CONTAINER:-}" ]; then
    docker exec "$DB_CONTAINER" "$@"
  else
    docker compose exec -T "$service" "$@"
  fi
}
sql() { in_db psql -U postgres -v ON_ERROR_STOP=1 -At -d "$1" -c "$2"; }

# Everything that makes two databases the same, read as the superuser (row security never applies).
fingerprint() {
  sql "$1" "
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
    ORDER BY 1"
}

echo "backing up $source_db"
in_db pg_dump -U postgres --format=custom --file="$dump" "$source_db"

echo "restoring into $copy, made as docs/runbooks/restore.md makes a database"
sql postgres "DROP DATABASE IF EXISTS $copy"
in_db psql -U postgres -v ON_ERROR_STOP=1 -q -d postgres -v db="$copy" \
  -f /opt/finops/sql/create-database.sql
sql postgres "GRANT TEMPORARY ON DATABASE $copy TO migrator"
in_db pg_restore -U postgres --single-transaction --exit-on-error --dbname="$copy" "$dump"

source_print="$(fingerprint "$source_db")"
copy_print="$(fingerprint "$copy")"
if [ "$source_print" != "$copy_print" ]; then
  echo "the restored copy differs from the source:" >&2
  diff <(echo "$source_print") <(echo "$copy_print") >&2 || true
  exit 1
fi
tables="$(grep -c '^rows ' <<<"$source_print")"
rows="$(awk '$1 == "rows" { n += $3 } END { printf "%.0f", n }' <<<"$source_print")"
ledger="$(awk '$1 == "ledger" { e += $3; c += $5 } END { printf "%.0f events, %.0f micro-USD", e, c }' \
  <<<"$source_print")"
echo "restore drill passed: $tables tables and $rows rows are identical by count and content;"
echo "the ledger holds $ledger in both; schema, grants, row security and policies match"
sql postgres "DROP DATABASE $copy"
in_db rm -f "$dump"
