# Runbook: PostgreSQL 19 pre-release to GA

The stack runs PostgreSQL 19 RC1, pinned by digest in `infra/docker/postgres/Dockerfile`. GA is
planned for 2026-11-19, after RC2 on 2026-11-12. Rehearse the move on a copy, then make it.

## 1. Pick the method

Compare the catalog versions:

```bash
docker compose exec -T db psql -U postgres -Atc "SELECT catalog_version_no FROM pg_control_system()"
docker run -d --rm --name pg-ga -e POSTGRES_PASSWORD=throwaway <GA image>
docker exec pg-ga pg_isready -U postgres -t 30
docker exec pg-ga psql -U postgres -Atc "SELECT catalog_version_no FROM pg_control_system()"
docker stop pg-ga
```

- **Same catalog version** (usual from the last RC to GA): swap the binaries.
  1. Re-pin `FROM`, `PGVECTOR_VERSION` and the pg_partman package together, then rebuild.
  2. Stop the cluster and start the new image on the same volume.
  3. Run `ALTER EXTENSION vector UPDATE; ALTER EXTENSION pg_partman UPDATE;` in `finops`.
- **Different catalog version**: dump and restore. Take a backup with the RC's `pg_dump`, start the
  GA image on a new volume, and follow `restore.md`. The weekly restore drill keeps that path
  tested.
  - `pg_upgrade --link` is faster on a large database, but it needs both builds' binaries in one
    container. No published image has that for one major version: install the GA packages, then
    copy the RC's `/usr/lib/postgresql/19` to another path.

## 2. Rehearse on a copy

1. Restore last night's backup into a scratch environment. Use `scripts/env.sh up staging` with
   the RC image, then `restore.md`.
2. Apply the method you picked.
3. Push the re-pinned Dockerfile to a branch. The whole CI suite must pass on GA: migrations from
   empty, the RLS audit and the integration tests.
4. Run the `bench` workflow (the latency gates of spec section 11) and the `load` workflow, which
   ends with the restore drill. Compare their numbers with the RC runs in `docs/audits/`.

## 3. Upgrade production

1. Take a fresh backup. Run the restore drill on it.
2. Announce a maintenance window. A binaries swap takes one restart; a dump and restore takes as
   long as the drill took on this database.
3. Stop the writers (gateway, worker). Upgrade, start them again, and check `/readyz`.
4. Watch the error rate and latency for an hour. To roll back, restore the step 1 backup on the RC
   image.

Status: blocked until GA ships. PROGRESS tracks it.
