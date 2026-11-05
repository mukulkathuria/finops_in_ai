# Runbook: backups, restore and the restore drill

## What to back up

| What | How | Why |
| --- | --- | --- |
| The database | `pg_dump --format=custom` nightly; in production also continuous WAL archiving (pgBackRest or WAL-G) for point-in-time recovery | Everything: orgs, key hashes, budgets, the ledger, documents, the audit chain |
| Cluster roles | Nothing to copy: the database image creates `migrator`, `app`, `worker` and `readonly_analytics` on first start (`infra/docker/postgres/sql/roles.sql`) | The dump refers to them by name |
| Stored files | The object store volume (`/var/lib/finops/objects`) | Uploaded originals and their Markdown |
| Secrets | `MASTER_KEY`, every `MASTER_PREVIOUS_KEYS` entry, `JWT_SIGNING_KEY`, kept apart from the backups | Without the master keys, sealed provider keys, webhook and single sign-on secrets cannot be opened |

Store backups encrypted, in another account or region than the database. A backup that has never
been restored is not a backup. Run the drill below after every schema change, and at least monthly.

## Restore

Never drop anything until the restored copy has been checked: restore next to the live database,
then swap names.

1. Provision the environment (`docs/runbooks/deploy.md`) with the same `MASTER_KEY`,
   `MASTER_PREVIOUS_KEYS` and `JWT_SIGNING_KEY`. Restore the object store volume.
2. Stop the writers: `docker compose stop gateway worker`.
3. Restore into a new database made by the image's own script. A plain dump carries no
   database-level grants, and `database-setup.sql` would clash with the dump's extensions:

   ```bash
   docker compose cp finops.dump db:/tmp/finops.dump
   docker compose exec -T db psql -U postgres -v ON_ERROR_STOP=1 -d postgres \
     -v db=finops_restored -f /opt/finops/sql/create-database.sql
   docker compose exec -T db psql -U postgres -d postgres \
     -c "GRANT TEMPORARY ON DATABASE finops_restored TO migrator"
   docker compose exec -T db pg_restore -U postgres --single-transaction --exit-on-error \
     --dbname=finops_restored /tmp/finops.dump
   ```

4. Swap it in, keeping the old one until the checks pass:

   ```bash
   docker compose exec -T db psql -U postgres -d postgres \
     -c "ALTER DATABASE finops RENAME TO finops_before_restore" \
     -c "ALTER DATABASE finops_restored RENAME TO finops"
   ```

5. Run the migrations, since a backup from before a release must catch up:
   `docker compose run --rm migrate`. Then `docker compose start gateway worker`.
6. Check the following:
   - `GET /readyz`;
   - one sign-in;
   - the audit chain, with `GET /v1/admin/audit/verify`;
   - one org's usage report against its ledger.

   When all of these pass, drop `finops_before_restore`.

## The drill

```bash
docker compose stop gateway worker
scripts/restore_drill.sh
docker compose start gateway worker
```

The drill backs up the database and restores the backup with the commands of step 3. Then it
compares the copy with the source:

- the migration, extensions, functions, indexes, constraints and triggers;
- the database, schema, table and column grants;
- forced row-level security and every policy;
- every sequence;
- every table, by row count and by a hash of its content;
- the ledger totals of every org (spec section 10).

It fails on any difference, and drops the copy when it passes. The `load` workflow runs it each
week, on the database the load profiles have just filled.
