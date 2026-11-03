---
name: write-migration
description: Write an Alembic migration with RLS, grants, indexes and a rollback path.
---
# write-migration

**Trigger:** any schema change.

**Inputs:** the table design, its hot queries, expected row counts.

**Steps**
1. `uv run alembic revision -m "<story>: <change>"`; write explicit SQL with `op.execute`.
2. Every tenant table: `org_id uuid NOT NULL`, `ENABLE` and `FORCE ROW LEVEL SECURITY`, a
   `tenant_isolation` policy with `USING` and `WITH CHECK` on `app_current_org()`.
3. Explicit GRANTs per role (app, worker, readonly_analytics). Never grant on partitions.
4. Primary keys `uuid DEFAULT uuidv7()`; money and tokens `bigint`; timestamps `timestamptz`.
5. Index every hot query; capture `EXPLAIN (ANALYZE, BUFFERS)` in the PR description.
6. Large tables: add columns nullable first, backfill in batches, then add constraints with
   `NOT VALID` plus `VALIDATE`. Create indexes `CONCURRENTLY` outside a transaction.
7. Write `downgrade()` and test upgrade, downgrade, upgrade from empty.

**Done when**
- [ ] `scripts/audit_rls.py` green
- [ ] Integration tests green on PostgreSQL 19
- [ ] Downgrade tested
