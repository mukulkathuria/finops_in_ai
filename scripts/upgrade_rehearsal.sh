#!/usr/bin/env bash
# pg_upgrade rehearsal (spec section 10: the PostgreSQL 18 to 19 upgrade with pg_upgrade, on a
# copy). The rehearsal:
# 1. builds the schema (the migrations) and data (the latency bench's seed, small) on 18;
# 2. moves the cluster to 19 with pg_upgrade --link;
# 3. proves the database the same afterwards: scripts/db_fingerprint.sql before and after, and
#    the RLS audit.
# docs/runbooks/postgres-upgrade.md uses the same steps to move from the 19 pre-release to GA.
#
#   scripts/upgrade_rehearsal.sh        needs Docker and uv; uses 127.0.0.1:5433
set -euo pipefail
cd "$(dirname "$0")/.."
old="${OLD_MAJOR:-18}"
new="${NEW_MAJOR:-19}"
name=finops-upgrade-rehearsal
port=5433
data=/var/lib/postgresql/rehearsal
pw=rehearsal # a throwaway cluster, published on this host's loopback only

docker build -q -t finops-postgres:ci infra/docker/postgres >/dev/null
docker build -q -t finops-postgres:upgrade --build-arg "OLD_MAJOR=$old" \
  -f infra/docker/postgres/upgrade.Dockerfile infra/docker/postgres >/dev/null
docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" -p "127.0.0.1:$port:5432" --entrypoint sleep \
  finops-postgres:upgrade infinity >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

as_pg() { docker exec -i -u postgres "$name" "$@"; }
bin() { echo "/usr/lib/postgresql/$1/bin"; }
psql_q() { as_pg psql -v ON_ERROR_STOP=1 -q -U postgres "$@"; }
fingerprint() { as_pg psql -v ON_ERROR_STOP=1 -At -U postgres -d finops < scripts/db_fingerprint.sql; }
cluster() {  # a new cluster of major $1, reachable from this host through the published port
  as_pg "$(bin "$1")/initdb" -D "$data/$1" -U postgres --auth=trust -E UTF8 >/dev/null
  as_pg sh -c "echo 'host all all all trust' >> '$data/$1/pg_hba.conf'"
}
start() { as_pg "$(bin "$1")/pg_ctl" -D "$data/$1" -o "-c listen_addresses='*'" -l "$data/$1.log" -w start >/dev/null; }

echo "PostgreSQL $old: roles, the database and its extensions, then the migrations"
as_pg mkdir -p "$data"
cluster "$old"
start "$old"
psql_q -d postgres -v migrator_pw=$pw -v app_pw=$pw -v worker_pw=$pw -v readonly_pw=$pw \
  -f /opt/finops/sql/roles.sql
psql_q -d postgres -v db=finops -f /opt/finops/sql/create-database.sql
psql_q -d finops -f /opt/finops/sql/database-setup.sql
MIGRATOR_DATABASE_URL="postgresql://migrator:$pw@127.0.0.1:$port/finops" \
  uv run alembic upgrade head 2>&1 | tail -1

echo "Seeding documents, vectors (HNSW per partition) and the code graph"
uv run python - "postgresql://postgres@127.0.0.1:$port/finops" <<'PY'
import sys

from tests.load.bench_context import Scale, seed

took = seed(sys.argv[1], Scale(orgs=5, chunks=4000, edges=4000, projects=4, samples=1, workers=2))
print("seeded:", ", ".join(f"{step} {seconds:.1f} s" for step, seconds in took.items()))
PY
psql_q -d finops -c "DROP TABLE bench_centroids" # the bench's own query helper, not product data
before="$(fingerprint)"
was="$(psql_q -d finops -At -c "SHOW server_version")"
as_pg "$(bin "$old")/pg_ctl" -D "$data/$old" -m fast -w stop >/dev/null

echo "pg_upgrade --link from $old to $new"
cluster "$new"
upgrade="cd '$data' && '$(bin "$new")/pg_upgrade' -b '$(bin "$old")' -B '$(bin "$new")'"
upgrade="$upgrade -d '$data/$old' -D '$data/$new' --link"
as_pg sh -c "$upgrade --check"
as_pg sh -c "$upgrade"
start "$new"
as_pg "$(bin "$new")/vacuumdb" -U postgres --all --analyze-only --quiet

after="$(fingerprint)"
now="$(psql_q -d finops -At -c "SHOW server_version")"
if [ "$before" != "$after" ]; then
  echo "the upgraded database differs:" >&2
  diff <(echo "$before") <(echo "$after") >&2 || true
  exit 1
fi
AUDIT_DATABASE_URL="postgresql://migrator:$pw@127.0.0.1:$port/finops" \
  uv run python scripts/audit_rls.py
tables="$(grep -c '^rows ' <<<"$after")"
rows="$(awk '$1 == "rows" { n += $3 } END { printf "%.0f", n }' <<<"$after")"
echo "pg_upgrade rehearsal passed: PostgreSQL $was to $now; $tables tables and $rows rows identical"
