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
    docker exec -i "$DB_CONTAINER" "$@"
  else
    docker compose exec -T "$service" "$@"
  fi
}
sql() { in_db psql -U postgres -v ON_ERROR_STOP=1 -At -d "$1" -c "$2"; }

# Everything that makes two databases the same, read as the superuser (row security never
# applies): scripts/db_fingerprint.sql, which the pg_upgrade rehearsal compares too.
fingerprint() {
  in_db psql -U postgres -v ON_ERROR_STOP=1 -At -d "$1" < scripts/db_fingerprint.sql
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
