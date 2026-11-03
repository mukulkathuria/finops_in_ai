# ADR 0002: Tenancy through forced RLS, request-scoped settings and four database roles

- Status: accepted
- Date: 2026-11-03
- Stories: US-01..US-13, US-28 (spec sections 4, 5, 10)

## Context
Every tenant row carries `org_id`. The spec requires Row-Level Security with `FORCE ROW LEVEL
SECURITY`, `SET LOCAL app.org_id / app.user_id` per transaction, and separate roles for migrations,
the application and analytics. Some reads are inherently pre-tenant (login by email, API-key and
refresh-token lookup) and some jobs are cross-tenant (outbox, reservation janitor, rollups).

## Decision
1. Roles (created once per cluster by `infra/docker/postgres/sql/roles.sql`):
   - `migrator` owns every object and runs Alembic. Not a superuser, no BYPASSRLS.
   - `app` serves requests. Not an owner, so RLS always applies.
   - `worker` runs background jobs. It sees other tenants only through explicit
     `FOR ... TO worker` policies on the specific tables a job needs. This is a fourth role beyond
     the three in the spec, so cross-tenant jobs never require weakening the `app` role.
   - `readonly_analytics` gets SELECT on reporting tables and stays under tenant RLS.
2. Tenant context: each transaction runs
   `SELECT set_config('app.org_id', :org, true), set_config('app.user_id', :user, true)`
   (the parameterisable form of `SET LOCAL`). Policies call `app_current_org()`, which maps a missing
   or empty setting to NULL, so an unset context returns zero rows instead of an error.
3. Pre-tenant lookups use narrow, SELECT-only policies keyed on a request-scoped setting that holds
   a hash or normalised email, for example
   `USING (key_hash = app_lookup('key_hash'))`. A caller can only read a row whose secret hash it
   already knows.
4. Every tenant table: `ENABLE` + `FORCE ROW LEVEL SECURITY`, a `tenant_isolation` policy with both
   `USING` and `WITH CHECK`, and explicit per-table GRANTs (no blanket default privileges).
   `scripts/audit_rls.py` fails CI if any table with an `org_id` column (or `orgs`) breaks this.
5. Partitioned tables: grants and policies live on the parent. Partitions are not granted to `app`,
   so they cannot be queried directly around the policies of the parent.

## Alternatives considered
- SECURITY DEFINER lookup functions owned by a BYPASSRLS role: needs a superuser to create the role
  and adds an ownership dance to every migration; the hash-keyed policy gives the same guarantee.
- One schema per tenant: strong isolation, but migrations and pooling scale badly past a few
  hundred tenants.

## Consequences
- RLS defends against application bugs (a forgotten `WHERE org_id`). It does not defend against an
  attacker who can run arbitrary SQL as `app`, because `set_config` is callable by that role. SQL
  injection is prevented separately: parameterised SQL only, enforced by Semgrep rules in CI.
- Tests must assert isolation through the real `app` role, never as the owner.
