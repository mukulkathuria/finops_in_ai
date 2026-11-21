# Runbook: upgrading PostgreSQL

The stack runs PostgreSQL 19.0, released 2026-11-19 and pinned by digest in
`infra/docker/postgres/Dockerfile`, with pgvector 0.8.7 and pg_partman 5.5.0 from PGDG.

## From the 19 release candidate to 19.0 (done)

The stack was built on 19 RC1. RC1, RC2 and 19.0 share one catalog version (`202610061`), so
the move was a swap of binaries: a data volume created under RC1 starts under 19.0 as it is,
with no dump, restore or `pg_upgrade`. To move an environment still on the RC image:

1. Take a backup (`restore.md`).
2. Rebuild the database image and restart it on the same volume: `docker compose up -d --build db`,
   or `scripts/env.sh up <env>` for a Terraform environment.
3. Check the server and the extensions:

   ```bash
   docker compose exec -T db psql -U postgres -d finops -Atc "SHOW server_version" \
     -c "ALTER EXTENSION vector UPDATE" -c "ALTER EXTENSION pg_partman UPDATE"
   ```

   The extension updates change nothing at 19.0: pgvector and pg_partman keep the versions they
   had on the RC.
4. Check `/readyz`.

PostgreSQL 19.0 ships without SQL/PGQ: no `CREATE PROPERTY GRAPH` and no `GRAPH_TABLE`. The
feature was reverted before beta 4 and is expected in PostgreSQL 20. The code graph keeps the
`GraphStore` built on joins and recursive CTEs (ADR 0004). `/readyz` and a CI test report whether
the server has SQL/PGQ.

## Minor releases (19.x)

A minor release keeps the catalog version, so it is always a swap of binaries:

1. Re-pin `FROM` by digest, together with `PGVECTOR_VERSION` and `PARTMAN_VERSION` (the PGDG
   builds for the new minor).
2. Push to a branch, so CI, the restore drill and the `upgrade` rehearsal run on the new image.
3. Run the `bench` and `load` workflows. Compare their numbers with `docs/audits/`.
4. Upgrade production as below, with the image swap as the method.

## Major releases (for example 19 to 20)

1. **Read the release notes' "Migration" section.** In 19, the notes that matter for an upgrade
   are these:
   - pg_trgm and full-text indexes under non-libc locale providers need a `REINDEX`. This stack
     uses the libc provider and has no such index.
   - `pg_upgrade` refuses btree_gist `inet`/`cidr` indexes. There are none here.
   - The default of `max_locks_per_transaction` doubled to 128. Settings copied from an older
     release need doubling too.
   - JIT is off by default, and `standard_conforming_strings` is always on.
2. **Pick the method.**
   - `pg_upgrade --link` takes minutes. `scripts/upgrade_rehearsal.sh` rehearses it every week:
     the database image with the older major installed beside it
     (`infra/docker/postgres/upgrade.Dockerfile`), every migration, seeded data with HNSW
     indexes, the upgrade, then the copy proved identical (`scripts/db_fingerprint.sql`) and the
     RLS audit clean. For the next major, re-pin the base image and run the rehearsal with
     `OLD_MAJOR=19 NEW_MAJOR=20`.
   - The fallback is dump and restore (`restore.md`), which the restore drill tests on every push.
3. **Rehearse on a copy.** Restore last night's backup into a scratch environment
   (`scripts/env.sh up staging`, then `restore.md`), apply the method, then run CI, the `bench`
   and the `load` workflows on the new image.

## Upgrade production

1. Take a fresh backup, and run the restore drill on it.
2. Announce a maintenance window. A swap of binaries takes one restart; `pg_upgrade --link` takes
   minutes; a dump and restore takes as long as the drill took on this database.
3. Stop the writers (gateway, worker). Upgrade, start them again, and check `/readyz`.
4. Watch the error rate and latency for an hour. To roll back, restore the step 1 backup on the
   previous image.
